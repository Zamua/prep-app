"""Anki .apkg export.

Inverse of anki.py — render a prep deck as a `.apkg` zip Anki can
import. Closes the round-trip: a user can pull their cards out into
Anki Desktop, AnkiDroid, AnkiMobile, or any other tool that reads the
format.

## Output shape

A minimal `.apkg` is:
    deck.apkg/
    ├── collection.anki21    (sqlite; new-schema, scheduler v2/v3)
    └── media                (JSON map "0":"filename.png" — we always
                              ship an empty `{}` since prep has no
                              media yet)

Inside the sqlite:
- `col` — 1 row, the collection header. Carries `models` JSON (a
  single "Basic" note type with two fields + one card template) and
  `decks` JSON (the deck we're exporting).
- `notes` — one row per prep card. `flds` is `prompt + chr(0x1f) + answer`.
- `cards` — one row per note (1:1 mapping). Brand-new cards: queue=0
  (new), due=note_id, ivl=0. Anki picks them up on import as never-
  studied.
- `revlog` — empty (we don't export review history).
- `graves` — empty (no deletions to replay).

The schema follows Anki's "anki21" format (post-2.1.50). It's
backward-compatible with older readers via the `schema_version=11`
field in `col`.

## What we drop

- Card type — prep has mcq/multi/code/short; Anki has only "basic"
  note types out of the box. We flatten everything to a basic two-
  field note. The mcq/multi structure is preserved by joining
  choices into the prompt field.
- SRS state (FSRS stability/difficulty, due times) — Anki uses its
  own scheduler; exporting our internal state would mislead. New
  cards in the imported deck.
- explanation / rubric / topic — these go inline into the answer
  field as labeled sections so no content is lost.

Wire-format reference: https://docs.ankiweb.net/exporting.html +
https://github.com/ankitects/anki/blob/main/rslib/src/storage/sqlite.rs
"""

from __future__ import annotations

import io
import json
import logging
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path

from prep.decks.entities import Question, QuestionType
from prep.decks.io import _questions_for_export
from prep.decks.repo import DeckRepo

logger = logging.getLogger(__name__)


# The model ID and deck ID are stable per-export (millisecond
# timestamps in the real Anki, but any positive int works — they
# only need to be unique within the .apkg).
_MODEL_ID = 1_700_000_000_000
_DECK_ID = 1_700_000_000_001


def _build_question_body(q: Question) -> tuple[str, str]:
    """Render a prep Question as an Anki (front, back) pair. Anki
    note types support HTML, but we emit plain text + minimal `<br>`
    line breaks — keeps output Anki-mobile-friendly."""
    front = q.prompt
    # MCQ/multi choices go on the front: the user needs them to
    # answer.
    if q.choices and q.type in (QuestionType.MCQ, QuestionType.MULTI):
        front = front + "<br><br>" + "<br>".join(f"• {c}" for c in q.choices)

    back_parts = [q.answer]
    if q.explanation:
        back_parts.append(f"<br><br><b>Explanation:</b><br>{q.explanation}")
    if q.rubric and q.type == QuestionType.CODE:
        back_parts.append(f"<br><br><b>Rubric:</b><br>{q.rubric}")
    if q.skeleton and q.type == QuestionType.CODE:
        back_parts.append(
            f"<br><br><b>Skeleton ({q.language or 'code'}):</b><br><pre>{q.skeleton}</pre>"
        )
    if q.topic:
        back_parts.append(f"<br><br><i>Topic: {q.topic}</i>")
    back = "".join(back_parts)
    # Anki's flds column escapes newlines → <br>. We've already
    # used explicit <br> above so a literal newline pass-through is
    # fine.
    front = front.replace("\n", "<br>")
    back = back.replace("\n", "<br>")
    return front, back


def _col_payload(deck_name: str, now_ms: int) -> dict[str, str]:
    """Build the JSON blobs that go into the `col` row. Anki refuses
    to import a collection whose `models` and `decks` JSON doesn't
    cover every note/card referenced — so this has to stay tight to
    the writes below.
    """
    # The single-note-type model. Fields: Front, Back. One card
    # template (Front → Back) so each note generates exactly one
    # card.
    models = {
        str(_MODEL_ID): {
            "id": _MODEL_ID,
            "name": "Basic (prep export)",
            "type": 0,  # standard, not cloze
            "mod": now_ms // 1000,
            "usn": -1,
            "sortf": 0,
            "did": _DECK_ID,
            "tmpls": [
                {
                    "name": "Card 1",
                    "ord": 0,
                    "qfmt": "{{Front}}",
                    "afmt": '{{FrontSide}}<hr id="answer">{{Back}}',
                    "bqfmt": "",
                    "bafmt": "",
                    "did": None,
                    "bfont": "",
                    "bsize": 0,
                }
            ],
            "flds": [
                {
                    "name": "Front",
                    "ord": 0,
                    "sticky": False,
                    "rtl": False,
                    "font": "Arial",
                    "size": 20,
                },
                {
                    "name": "Back",
                    "ord": 1,
                    "sticky": False,
                    "rtl": False,
                    "font": "Arial",
                    "size": 20,
                },
            ],
            "css": ".card { font-family: arial; font-size: 20px; text-align: left; color: black; background: white; }",
            "latexPre": "",
            "latexPost": "",
            "req": [[0, "any", [0]]],
        }
    }
    decks = {
        "1": {  # the default deck
            "id": 1,
            "name": "Default",
            "mod": now_ms // 1000,
            "usn": -1,
            "lrnToday": [0, 0],
            "revToday": [0, 0],
            "newToday": [0, 0],
            "timeToday": [0, 0],
            "collapsed": False,
            "browserCollapsed": False,
            "desc": "",
            "dyn": 0,
            "conf": 1,
            "extendNew": 10,
            "extendRev": 50,
        },
        str(_DECK_ID): {
            "id": _DECK_ID,
            "name": deck_name,
            "mod": now_ms // 1000,
            "usn": -1,
            "lrnToday": [0, 0],
            "revToday": [0, 0],
            "newToday": [0, 0],
            "timeToday": [0, 0],
            "collapsed": False,
            "browserCollapsed": False,
            "desc": f"Exported from prep on {time.strftime('%Y-%m-%d')}",
            "dyn": 0,
            "conf": 1,
            "extendNew": 10,
            "extendRev": 50,
        },
    }
    dconf = {
        "1": {
            "id": 1,
            "name": "Default",
            "replayq": True,
            "lapse": {
                "leechFails": 8,
                "minInt": 1,
                "delays": [10],
                "leechAction": 0,
                "mult": 0,
            },
            "rev": {
                "perDay": 100,
                "ease4": 1.3,
                "fuzz": 0.05,
                "minSpace": 1,
                "ivlFct": 1,
                "maxIvl": 36500,
                "bury": False,
            },
            "timer": 0,
            "maxTaken": 60,
            "usn": -1,
            "new": {
                "perDay": 20,
                "delays": [1, 10],
                "separate": True,
                "ints": [1, 4, 7],
                "initialFactor": 2500,
                "bury": False,
                "order": 1,
            },
            "mod": 0,
            "autoplay": True,
        }
    }
    conf = {
        "nextPos": 1,
        "estTimes": True,
        "activeDecks": [1],
        "sortType": "noteFld",
        "timeLim": 0,
        "sortBackwards": False,
        "addToCur": True,
        "curDeck": 1,
        "newBury": True,
        "newSpread": 0,
        "dueCounts": True,
        "curModel": _MODEL_ID,
        "collapseTime": 1200,
    }
    return {
        "models": json.dumps(models),
        "decks": json.dumps(decks),
        "dconf": json.dumps(dconf),
        "conf": json.dumps(conf),
        "tags": "{}",
    }


def _init_anki_db(conn: sqlite3.Connection) -> None:
    """Create the tables Anki expects in `collection.anki21`. Schema
    cribbed from anki/rslib/src/storage/sqlite.rs — keep it minimal
    but complete; importers refuse unfamiliar schemas."""
    conn.executescript(
        """
        CREATE TABLE col (
            id              INTEGER PRIMARY KEY,
            crt             INTEGER NOT NULL,
            mod             INTEGER NOT NULL,
            scm             INTEGER NOT NULL,
            ver             INTEGER NOT NULL,
            dty             INTEGER NOT NULL,
            usn             INTEGER NOT NULL,
            ls              INTEGER NOT NULL,
            conf            TEXT    NOT NULL,
            models          TEXT    NOT NULL,
            decks           TEXT    NOT NULL,
            dconf           TEXT    NOT NULL,
            tags            TEXT    NOT NULL
        );
        CREATE TABLE notes (
            id      INTEGER PRIMARY KEY,
            guid    TEXT    NOT NULL,
            mid     INTEGER NOT NULL,
            mod     INTEGER NOT NULL,
            usn     INTEGER NOT NULL,
            tags    TEXT    NOT NULL,
            flds    TEXT    NOT NULL,
            sfld    INTEGER NOT NULL,
            csum    INTEGER NOT NULL,
            flags   INTEGER NOT NULL,
            data    TEXT    NOT NULL
        );
        CREATE TABLE cards (
            id      INTEGER PRIMARY KEY,
            nid     INTEGER NOT NULL,
            did     INTEGER NOT NULL,
            ord     INTEGER NOT NULL,
            mod     INTEGER NOT NULL,
            usn     INTEGER NOT NULL,
            type    INTEGER NOT NULL,
            queue   INTEGER NOT NULL,
            due     INTEGER NOT NULL,
            ivl     INTEGER NOT NULL,
            factor  INTEGER NOT NULL,
            reps    INTEGER NOT NULL,
            lapses  INTEGER NOT NULL,
            left    INTEGER NOT NULL,
            odue    INTEGER NOT NULL,
            odid    INTEGER NOT NULL,
            flags   INTEGER NOT NULL,
            data    TEXT    NOT NULL
        );
        CREATE TABLE revlog (
            id      INTEGER PRIMARY KEY,
            cid     INTEGER NOT NULL,
            usn     INTEGER NOT NULL,
            ease    INTEGER NOT NULL,
            ivl     INTEGER NOT NULL,
            lastIvl INTEGER NOT NULL,
            factor  INTEGER NOT NULL,
            time    INTEGER NOT NULL,
            type    INTEGER NOT NULL
        );
        CREATE TABLE graves (
            usn  INTEGER NOT NULL,
            oid  INTEGER NOT NULL,
            type INTEGER NOT NULL
        );
        CREATE INDEX ix_notes_csum ON notes (csum);
        CREATE INDEX ix_cards_nid ON cards (nid);
        CREATE INDEX ix_cards_sched ON cards (did, queue, due);
        CREATE INDEX ix_revlog_cid ON revlog (cid);
        """
    )


def deck_to_apkg(user_id: str, deck_id: int, deck_name: str) -> bytes:
    """Render `deck_id` as a `.apkg` zip Anki can import. Returns
    the raw bytes (caller handles Content-Disposition).

    Note: writes to a NamedTemporaryFile because sqlite3 can't open
    in-memory by path, then reads the bytes back and cleans up.
    """
    questions = _questions_for_export(user_id, deck_id)
    now_ms = int(time.time() * 1000)
    now_s = now_ms // 1000

    sqlite_tf = tempfile.NamedTemporaryFile(suffix=".anki21", delete=False)
    sqlite_tf.close()
    try:
        conn = sqlite3.connect(sqlite_tf.name)
        try:
            _init_anki_db(conn)
            payload = _col_payload(deck_name, now_ms)
            conn.execute(
                """INSERT INTO col (id, crt, mod, scm, ver, dty, usn, ls,
                                    conf, models, decks, dconf, tags)
                       VALUES      (1,  ?,   ?,   ?,   11,  0,   -1,  0,
                                    ?,    ?,      ?,     ?,     ?)""",
                (
                    now_s,
                    now_s,
                    now_ms,
                    payload["conf"],
                    payload["models"],
                    payload["decks"],
                    payload["dconf"],
                    payload["tags"],
                ),
            )

            for idx, q in enumerate(questions):
                front, back = _build_question_body(q)
                flds = front + "\x1f" + back
                note_id = now_ms + idx
                # csum is Anki's "first 8 hex chars of SHA1(sortf)" — a
                # dedup hint, integer. Cheap stand-in: hash the front.
                csum = abs(hash(front)) % (10**10)
                # Anki uses a string GUID; the only requirement is
                # uniqueness within the deck. Stable per-card so
                # re-import dedups.
                guid = f"prep-{user_id[:8]}-{q.id}"

                conn.execute(
                    """INSERT INTO notes (id, guid, mid, mod, usn, tags,
                                          flds, sfld, csum, flags, data)
                       VALUES (?,  ?,    ?,   ?,   -1,  '',
                               ?,    ?,    ?,    0,     '')""",
                    (note_id, guid, _MODEL_ID, now_s, flds, front[:200], csum),
                )

                card_id = now_ms + 100000 + idx
                conn.execute(
                    """INSERT INTO cards (id, nid, did, ord, mod, usn,
                                          type, queue, due, ivl, factor,
                                          reps, lapses, left, odue, odid,
                                          flags, data)
                       VALUES (?,  ?,   ?,   0,   ?,   -1,
                               0,    0,     ?,   0,   0,
                               0,    0,      0,    0,    0,
                               0,     '')""",
                    (card_id, note_id, _DECK_ID, now_s, idx + 1),
                )

            conn.commit()
        finally:
            conn.close()

        # Zip into an in-memory .apkg.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(sqlite_tf.name, "collection.anki21")
            # Empty media map — required by Anki even when no media.
            zf.writestr("media", "{}")
        return buf.getvalue()
    finally:
        Path(sqlite_tf.name).unlink(missing_ok=True)


def export_deck_apkg_by_name(user_id: str, deck_name: str) -> bytes | None:
    """User-friendly entry — resolve deck name → id under the user,
    return None if not found (caller renders 404)."""
    deck_id = DeckRepo().find_id(user_id, deck_name)
    if deck_id is None:
        return None
    return deck_to_apkg(user_id, deck_id, deck_name)
