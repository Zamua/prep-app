"""CSV import/export for decks.

The wire format is a single CSV per deck. Columns chosen to round-trip
through prep losslessly AND to be friendly to Anki's "Notes in Plain
Text" exporter (Anki accepts arbitrary CSV with a Front/Back header).

The columns in order:
    type, topic, prompt, answer, choices, rubric, skeleton,
    language, answer_regex, explanation

`choices` is newline-joined within the cell (CSV quoting handles
embedded newlines correctly). Empty cells round-trip as Python None.

## Optional deck-level preamble

Trivia decks carry per-deck config that can't fit in per-card rows
(notification cadence, the topic prompt, session size). For those, the
exporter prepends `# key: value` lines BEFORE the CSV header:

    # deck_type: trivia
    # notification_interval_minutes: 30
    # topic_prompt: world capitals
    # trivia_session_size: 3
    type,topic,prompt,answer,...

The importer strips and parses these on read, then dispatches to the
right deck-creation path. SRS exports omit the preamble entirely so
the file is back-compat with pre-preamble readers (and with Anki's
plain-CSV import, which has no notion of comment lines). Any unknown
preamble keys are ignored so the format can grow without breaking
older readers.

This module is the single source of truth for the wire format — the
public API's `/api/v1/decks/<name>/export.csv` and the settings-page
"Export deck" button both call into here.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from prep.decks.entities import NewQuestion, Question, QuestionType
from prep.decks.repo import DeckRepo, QuestionRepo

# Stable column order. New columns get APPENDED, never inserted in the
# middle, so a CSV exported pre-change still imports post-change.
CSV_COLUMNS = (
    "type",
    "topic",
    "prompt",
    "answer",
    "choices",
    "rubric",
    "skeleton",
    "language",
    "answer_regex",
    "explanation",
)


def _question_to_row(q: Question) -> dict[str, str]:
    """Render a Question entity as a flat dict the csv writer accepts.
    None → empty string (csv DictWriter doesn't differentiate; we'll
    re-coerce on read).
    """
    return {
        "type": q.type.value,
        "topic": q.topic or "",
        "prompt": q.prompt,
        "answer": q.answer,
        # Newline-joined; CSV quoting wraps the whole cell so embedded
        # newlines round-trip cleanly. Anki accepts this shape too.
        "choices": "\n".join(q.choices) if q.choices else "",
        "rubric": q.rubric or "",
        "skeleton": q.skeleton or "",
        "language": q.language or "",
        "answer_regex": q.answer_regex or "",
        "explanation": q.explanation or "",
    }


def _build_preamble(user_id: str, deck_id: int) -> str:
    """Build the optional `# key: value` preamble emitted before the
    CSV header. Only non-empty for trivia decks today — SRS exports
    have no deck-level state worth preserving in the file (the FSRS
    state on individual cards isn't exported either; reimporting
    always starts at step 0)."""
    from prep.infrastructure.db import cursor

    with cursor() as c:
        row = c.execute(
            """SELECT COALESCE(deck_type, 'srs') AS deck_type,
                      notification_interval_minutes, trivia_session_size,
                      context_prompt
                 FROM decks WHERE id = ? AND user_id = ?""",
            (deck_id, user_id),
        ).fetchone()
    if row is None or row["deck_type"] != "trivia":
        return ""
    lines = ["# deck_type: trivia"]
    if row["notification_interval_minutes"] is not None:
        lines.append(
            f"# notification_interval_minutes: {int(row['notification_interval_minutes'])}"
        )
    if row["trivia_session_size"] is not None:
        lines.append(f"# trivia_session_size: {int(row['trivia_session_size'])}")
    if row["context_prompt"]:
        # Single-line: collapse any embedded newlines so the preamble
        # stays one-key-per-line. The topic prompt is usually a short
        # phrase but the column allows long text; trim to keep things
        # sane on round-trip (the trivia worker uses the prompt at
        # most a few hundred chars anyway).
        topic = row["context_prompt"].replace("\n", " ").replace("\r", " ").strip()
        lines.append(f"# topic_prompt: {topic}")
    return "\n".join(lines) + "\n"


def deck_to_csv(user_id: str, deck_id: int) -> str:
    """Render every question in `deck_id` as a CSV string. Caller is
    responsible for ownership (deck_id is already user-scoped) and
    for the response Content-Type header.

    For trivia decks, prepends a `# key: value` preamble carrying
    deck-level config so the importer can recreate the deck shape on
    the other side. SRS decks get no preamble (the file is plain CSV)."""
    preamble = _build_preamble(user_id, deck_id)
    questions = _questions_for_export(user_id, deck_id)
    buf = io.StringIO()
    if preamble:
        buf.write(preamble)
    writer = csv.DictWriter(buf, fieldnames=list(CSV_COLUMNS), extrasaction="ignore")
    writer.writeheader()
    for q in questions:
        writer.writerow(_question_to_row(q))
    return buf.getvalue()


def _split_preamble(csv_text: str) -> tuple[dict[str, str], str]:
    """Pull leading `# key: value` lines off the top of `csv_text`.
    Returns `(preamble_dict, csv_remainder)`.

    Stops at the first non-`#` non-blank line. Blank lines inside the
    preamble are skipped silently. Unknown keys are kept in the dict
    but ignored by the importer — keeps the format forward-compat as
    new keys land. Values are stripped; keys are lowercased.
    """
    preamble: dict[str, str] = {}
    lines = csv_text.splitlines(keepends=False)
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue
        if stripped.startswith("#"):
            kv = stripped[1:].strip()
            if ":" in kv:
                k, _, v = kv.partition(":")
                preamble[k.strip().lower()] = v.strip()
            i += 1
            continue
        break
    return preamble, "\n".join(lines[i:])


def _questions_for_export(user_id: str, deck_id: int) -> list[Question]:
    """Full Question entities for a deck, in stable order.

    Repo's `list_in_deck` returns DeckCards (joined with SRS state +
    drops explanation). For export we want the full Question shape —
    a focused query is clearer than threading the new field through
    DeckCard's existing readers.
    """
    from prep.infrastructure.db import cursor

    with cursor() as c:
        rows = c.execute(
            """
            SELECT id, user_id, deck_id, type, topic, prompt, answer,
                   choices, rubric, skeleton, language, answer_regex,
                   explanation, created_at, suspended
              FROM questions
             WHERE deck_id = ? AND user_id = ?
             ORDER BY id ASC
            """,
            (deck_id, user_id),
        ).fetchall()

    from prep.decks.repo import _row_to_question

    return [_row_to_question(dict(r)) for r in rows]


# ---- import ---------------------------------------------------------------


@dataclass(frozen=True)
class ImportOutcome:
    """Result of a CSV import. `inserted` is the count of new
    questions actually written; `skipped_duplicates` are rows whose
    prompt already existed in the target deck (silent skip — same as
    the trivia batch generator's dedup); `errors` are rows that
    failed validation, with one human-readable message per row."""

    deck_id: int
    deck_name: str
    inserted: int
    skipped_duplicates: int
    errors: list[str]


def csv_to_deck(
    user_id: str,
    deck_name: str,
    csv_text: str,
    *,
    deck_repo: DeckRepo,
    question_repo: QuestionRepo,
    context_prompt: str | None = None,
) -> ImportOutcome:
    """Create-or-find a deck, parse the CSV, insert questions.

    - Optional `# key: value` preamble lines before the header carry
      deck-level metadata (trivia decks use this; SRS exports omit
      it). Unknown keys are ignored.
    - Header is required (column names map to the `_question_to_row`
      shape). Extra columns are ignored. Missing columns default to
      empty.
    - Per-row validation surfaces as `errors` entries; valid rows
      still get inserted, so a single bad row doesn't sink the
      import.
    - Dedup is per (deck, prompt). Already-present prompts increment
      `skipped_duplicates` without raising. Same shape as the trivia
      batch generator's dedup so the two paths are interchangeable.

    Trivia-specific:
    - If the preamble says `deck_type: trivia`, the importer creates
      the deck as type=trivia (with `notification_interval_minutes`,
      `trivia_session_size`, `context_prompt` from the preamble) and
      appends each new question to `trivia_queue`.
    - If a deck with this name already exists with a DIFFERENT type
      than the preamble declares, the import fails with a clear
      `errors` entry rather than silently mixing shapes.
    """
    preamble, csv_text = _split_preamble(csv_text)
    declared_type = (preamble.get("deck_type") or "srs").lower()

    # Resolve / create the deck, gated by deck_type.
    existing_id = deck_repo.find_id(user_id, deck_name)
    if existing_id is not None:
        existing_type = deck_repo.get_type(user_id, existing_id)
        existing_type_str = existing_type.value if existing_type else "srs"
        if existing_type_str != declared_type:
            return ImportOutcome(
                deck_id=existing_id,
                deck_name=deck_name,
                inserted=0,
                skipped_duplicates=0,
                errors=[
                    f"deck '{deck_name}' already exists as "
                    f"{existing_type_str!r}; CSV declares {declared_type!r}. "
                    "Pick a different name or import into a fresh deck."
                ],
            )
        deck_id = existing_id
    else:
        if declared_type == "trivia":
            try:
                interval = int(preamble.get("notification_interval_minutes", "30"))
            except (TypeError, ValueError):
                interval = 30
            topic = preamble.get("topic_prompt") or context_prompt or ""
            deck_id = deck_repo.create_trivia(
                user_id, deck_name, topic=topic, interval_minutes=interval
            )
            # Optional session size override.
            try:
                if "trivia_session_size" in preamble:
                    size = int(preamble["trivia_session_size"])
                    deck_repo.set_trivia_session_size(user_id, deck_id, size)
            except (TypeError, ValueError):
                pass
        else:
            deck_id = deck_repo.get_or_create(user_id, deck_name)

    if context_prompt and declared_type != "trivia":
        # Only set if the deck is new-ish (no prompt yet) — don't
        # clobber a real one. Trivia handled above via preamble.
        existing_prompt = deck_repo.get_context_prompt(user_id, deck_name)
        if not existing_prompt:
            deck_repo.update_context_prompt(user_id, deck_name, context_prompt)

    existing = set()
    # Pull current prompts so we can dedup without round-tripping
    # one INSERT per row.
    with _cursor() as c:
        rows = c.execute(
            "SELECT prompt FROM questions WHERE deck_id = ? AND user_id = ?",
            (deck_id, user_id),
        ).fetchall()
        for r in rows:
            existing.add(r["prompt"])

    inserted = 0
    skipped_duplicates = 0
    errors: list[str] = []

    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        return ImportOutcome(
            deck_id=deck_id,
            deck_name=deck_name,
            inserted=0,
            skipped_duplicates=0,
            errors=["CSV has no header row"],
        )

    for i, row in enumerate(reader, start=2):  # row 1 is the header
        prompt = (row.get("prompt") or "").strip()
        if not prompt:
            errors.append(f"row {i}: missing prompt")
            continue
        if prompt in existing:
            skipped_duplicates += 1
            continue

        type_raw = (row.get("type") or "").strip().lower() or "short"
        try:
            qtype = QuestionType(type_raw)
        except ValueError:
            errors.append(f"row {i}: unknown type {type_raw!r}")
            continue

        answer = (row.get("answer") or "").strip()
        if not answer:
            errors.append(f"row {i}: missing answer")
            continue

        choices_raw = (row.get("choices") or "").strip()
        choices = [ln.strip() for ln in choices_raw.splitlines() if ln.strip()] or None

        try:
            new = NewQuestion(
                type=qtype,
                topic=(row.get("topic") or "").strip() or None,
                prompt=prompt,
                answer=answer,
                choices=choices,
                rubric=(row.get("rubric") or "").strip() or None,
                skeleton=(row.get("skeleton") or "").strip() or None,
                language=(row.get("language") or "").strip() or None,
                answer_regex=(row.get("answer_regex") or "").strip() or None,
                explanation=(row.get("explanation") or "").strip() or None,
            )
        except Exception as e:  # noqa: BLE001 — pydantic validation surface
            errors.append(f"row {i}: {e}")
            continue

        try:
            qid = question_repo.add(user_id, deck_id, new)
            existing.add(prompt)
            inserted += 1
            # Trivia: append to the per-deck queue so the question
            # becomes pickable. QuestionRepo.add() for trivia decks
            # skips the FSRS cards row (fix #335) — without an explicit
            # trivia_queue insert here the imported card would be dead
            # weight in the questions table.
            if declared_type == "trivia":
                from prep.trivia.repo import TriviaQueueRepo

                TriviaQueueRepo().append_card(qid, deck_id)
        except Exception as e:  # noqa: BLE001
            errors.append(f"row {i}: write failed — {e}")

    return ImportOutcome(
        deck_id=deck_id,
        deck_name=deck_name,
        inserted=inserted,
        skipped_duplicates=skipped_duplicates,
        errors=errors,
    )


def _cursor():
    """Late-imported cursor — keeps the io module importable in a
    pyproject-driven dependency-graph analyzer that hasn't seen
    sqlite yet."""
    from prep.infrastructure.db import cursor

    return cursor()
