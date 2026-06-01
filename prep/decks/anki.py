"""Anki .apkg import.

The .apkg format is the de-facto interchange for SRS decks: it's a
zip archive containing a sqlite database (`collection.anki2` or
`collection.anki21`) plus media files. Tens of thousands of decks
on ankiweb use it, and every serious flashcard tool can read one.
Supporting import is the cheapest "we play with the rest of the
ecosystem" signal.

## Scope (MVP)

- Read either `collection.anki21` (newer scheduler) or `collection.anki2`.
- One note → one prep short-type card.
- First field → prompt; remaining fields joined → answer.
- HTML is stripped (Anki stores rich text; prep stores plain text).
- Media references (`<img src=...>`, `[sound:...]`, `[anki:...]`) are
  dropped — we don't ingest the media payload in v1.
- Cloze cards (`{{c1::...}}` markers) are skipped, surfaced in errors.
- Anki decks are flattened: every note in the .apkg lands in the
  single prep deck the user named. Anki supports deck nesting; we
  don't, so collapsing is intentional.

## Out of scope (for now)

- Media unpacking into prep — the user can re-add images by editing
  the card.
- Per-note-type custom templates — Anki's model system is much
  richer than prep's; flattening to short-type loses fidelity but
  preserves the content.
- Anki's review history (ivl, factor, due) — prep starts every
  imported card fresh. The user is importing content, not progress.

This module is the single source of truth for the import wire path;
both the UI route (`/decks/import-anki`) and the future MCP tool
share it.
"""

from __future__ import annotations

import contextlib
import io
import logging
import re
import sqlite3
import tempfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from prep.decks.entities import NewQuestion, QuestionType
from prep.decks.repo import DeckRepo, QuestionRepo

logger = logging.getLogger(__name__)

# Anki field separator in the `flds` column — record separator
# (0x1f). Per Anki source: anki/rslib/src/notes/mod.rs.
_FIELD_SEP = "\x1f"

# Markers that indicate a cloze-deletion note. Cloze fields use
# `{{c1::hidden}}` syntax in the source field text; we don't try to
# resolve them, just skip.
_CLOZE_RE = re.compile(r"\{\{c\d+::")

# Coarse HTML stripper — Anki stores rich text in note fields and
# uses `<br>` for line breaks. We turn `<br>` and `</p>` into
# newlines first so structure is preserved, then strip the rest.
_BR_RE = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
_BLOCK_END_RE = re.compile(r"</\s*(?:p|div|li|h[1-6])\s*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_MEDIA_RE = re.compile(r"\[(?:sound|anki):[^\]]*\]", re.IGNORECASE)

# HTML entity unescape — covers the entities Anki actually emits.
_ENTITIES = {
    "&nbsp;": " ",
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&apos;": "'",
}


def _strip_html(s: str) -> str:
    if not s:
        return ""
    s = _BR_RE.sub("\n", s)
    s = _BLOCK_END_RE.sub("\n", s)
    s = _MEDIA_RE.sub("", s)
    s = _TAG_RE.sub("", s)
    for k, v in _ENTITIES.items():
        s = s.replace(k, v)
    # Collapse runs of blank lines and trim each line.
    lines = [ln.strip() for ln in s.splitlines()]
    out, prev_blank = [], False
    for ln in lines:
        if not ln:
            if prev_blank:
                continue
            prev_blank = True
        else:
            prev_blank = False
        out.append(ln)
    return "\n".join(out).strip()


@dataclass(frozen=True)
class AnkiImportOutcome:
    """Result shape parallels ImportOutcome from io.py — same JSON
    rendering, same UI partial. `cloze_skipped` is broken out from
    `errors` because it's expected (not a malformed file) but the
    user should know about it."""

    deck_id: int
    deck_name: str
    inserted: int
    skipped_duplicates: int
    cloze_skipped: int
    errors: list[str]


@contextlib.contextmanager
def _open_apkg_collection(blob: bytes) -> Iterator[sqlite3.Connection]:
    """Unzip the .apkg blob, find the collection sqlite, yield a
    read-only connection. Cleans up tempfile + closes the connection
    on exit.

    The zip may carry either `collection.anki21` (newer scheduler
    v2/v3 format) or `collection.anki2` (legacy). We prefer the
    newer one — Anki itself does.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as e:
        raise ValueError(f"not a valid .apkg (zip parse failed): {e}") from e

    names = set(zf.namelist())
    coll_name = None
    for candidate in ("collection.anki21", "collection.anki2"):
        if candidate in names:
            coll_name = candidate
            break
    if coll_name is None:
        raise ValueError("not a valid .apkg — no collection.anki2 / collection.anki21 inside")

    # sqlite3 needs a real file path; the python module doesn't open
    # in-memory blobs. Write to a tempfile, open, return — we delete
    # the tempfile after we close the connection.
    tf = tempfile.NamedTemporaryFile(suffix=".anki", delete=False)
    tempfile_path = tf.name
    try:
        tf.write(zf.read(coll_name))
        tf.flush()
        tf.close()
        conn = sqlite3.connect(f"file:{tempfile_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
    finally:
        Path(tempfile_path).unlink(missing_ok=True)


def apkg_to_deck(
    user_id: str,
    deck_name: str,
    blob: bytes,
    *,
    deck_repo: DeckRepo,
    question_repo: QuestionRepo,
) -> AnkiImportOutcome:
    """Parse an .apkg blob and append every importable note to
    `deck_name` as a short-type card. Returns counts + per-row
    errors; doesn't raise on individual bad notes."""
    deck_id = deck_repo.get_or_create(user_id, deck_name)

    # Existing prompts for dedup (same shape as csv_to_deck).
    from prep.infrastructure.db import cursor as _prep_cursor

    existing: set[str] = set()
    with _prep_cursor() as c:
        for r in c.execute(
            "SELECT prompt FROM questions WHERE deck_id = ? AND user_id = ?",
            (deck_id, user_id),
        ).fetchall():
            existing.add(r["prompt"])

    inserted = 0
    skipped_duplicates = 0
    cloze_skipped = 0
    errors: list[str] = []

    with _open_apkg_collection(blob) as conn:
        rows = conn.execute("SELECT id, flds FROM notes ORDER BY id ASC").fetchall()
        for note in rows:
            fields = (note["flds"] or "").split(_FIELD_SEP)
            if not fields or not fields[0]:
                errors.append(f"note {note['id']}: empty fields")
                continue

            raw_front = fields[0]
            if _CLOZE_RE.search(raw_front):
                cloze_skipped += 1
                continue

            prompt = _strip_html(raw_front)
            if not prompt:
                errors.append(f"note {note['id']}: empty prompt after HTML strip")
                continue
            if prompt in existing:
                skipped_duplicates += 1
                continue

            # Answer = remaining fields joined with blank line, HTML
            # stripped. If only one field, fall back to an empty
            # answer — the user can fill it in.
            back_raw = "\n\n".join(f for f in fields[1:] if f)
            answer = _strip_html(back_raw)
            if not answer:
                # Single-field notes (or all-other-fields empty)
                # aren't useful as a flashcard. Surface so the user
                # knows what was dropped.
                errors.append(f"note {note['id']}: no back-side content")
                continue

            try:
                new = NewQuestion(
                    type=QuestionType.SHORT,
                    prompt=prompt,
                    answer=answer,
                )
            except Exception as e:  # noqa: BLE001 — pydantic surface
                errors.append(f"note {note['id']}: {e}")
                continue

            try:
                question_repo.add(user_id, deck_id, new)
                existing.add(prompt)
                inserted += 1
            except Exception as e:  # noqa: BLE001
                errors.append(f"note {note['id']}: write failed — {e}")

    return AnkiImportOutcome(
        deck_id=deck_id,
        deck_name=deck_name,
        inserted=inserted,
        skipped_duplicates=skipped_duplicates,
        cloze_skipped=cloze_skipped,
        errors=errors,
    )
