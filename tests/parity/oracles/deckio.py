"""Deck interchange oracle: what the Python app writes and reads for
`.csv`, `.prepdeck` and `.apkg`.

`profiles.json` is the shared description of the decks under test, so
the TypeScript side builds the same rows through its own repositories
rather than the two sides drifting apart in two seed scripts. Run
standalone to regenerate:

    python -m tests.parity.oracles.deckio

Three tiers of comparison, and only two of them are bytes:

- `<p>.csv` and `<p>.prepdeck` are byte goldens. Both writers are
  deterministic in Python (`ZIP_STORED`, a fixed 1980 stamp,
  `json.dumps(indent=2, sort_keys=True)`).
- `<p>.apkg` is not, and Python is not byte-identical to itself:
  `csum` is `abs(hash(front))`, which CPython salts per process, and
  `zipfile.write` stamps the wall clock through a zlib build. So the
  golden is `<p>.apkg.dump.json`, a canonical dump of the collection,
  with `csum` the one excluded column.
- `<p>.import.json` records what the importers made of each corpus, so
  the other implementation is checked in both directions.
"""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

from tests.parity.oracles import PARITY_USER, REPO_ROOT, pin_clock
from tests.parity.oracles.harness import scratch_app

NAME = "deckio"
GOLDENS = REPO_ROOT / "tests" / "parity" / "goldens" / NAME
PROFILES_FILE = GOLDENS / "profiles.json"

# The one column whose value is not reproducible; see the module docstring.
EXCLUDED_NOTE_COLUMNS = ("csum",)

DUMPED_TABLES = ("col", "notes", "cards", "revlog", "graves")
# `col` carries JSON blobs; they are compared as structures, not as text.
COL_JSON_COLUMNS = ("conf", "models", "decks", "dconf", "tags")


def load_profiles() -> dict:
    return json.loads(PROFILES_FILE.read_text(encoding="utf-8"))


# ---- building a deck from the shared description --------------------------


def build_deck(user: str, spec: dict) -> int:
    """Insert one profile's deck through the repositories, in the order
    the TypeScript side inserts it, so ids line up."""
    from prep.decks.entities import NewQuestion, QuestionType
    from prep.decks.repo import DeckRepo, QuestionRepo
    from prep.study.repo import ReviewRepo
    from prep.trivia.repo import TriviaQueueRepo

    deck_repo, q_repo = DeckRepo(), QuestionRepo()
    review_repo, trivia_repo = ReviewRepo(), TriviaQueueRepo()

    deck = spec["deck"]
    if deck["type"] == "trivia":
        deck_id = deck_repo.create_trivia(
            user,
            deck["name"],
            topic=deck.get("context_prompt") or "",
            interval_minutes=int(deck.get("interval_minutes") or 30),
        )
        deck_repo.set_trivia_session_size(user, deck_id, int(deck.get("session_size") or 3))
    else:
        deck_id = deck_repo.create(user, deck["name"], context_prompt=deck.get("context_prompt"))
    if deck.get("desired_retention") is not None:
        deck_repo.set_desired_retention(user, deck_id, float(deck["desired_retention"]))

    for c in spec["cards"]:
        qid = q_repo.add(
            user,
            deck_id,
            NewQuestion(
                type=QuestionType(c["type"]),
                topic=c.get("topic"),
                prompt=c["prompt"],
                answer=c["answer"],
                choices=c.get("choices"),
                rubric=c.get("rubric"),
                skeleton=c.get("skeleton"),
                language=c.get("language"),
                answer_regex=c.get("answer_regex"),
                explanation=c.get("explanation"),
            ),
        )
        state = c.get("state")
        if state:
            review_repo.restore_card_state(
                qid,
                step=state.get("step"),
                next_due=state.get("next_due"),
                last_review=state.get("last_review"),
                stability=state.get("stability"),
                difficulty=state.get("difficulty"),
                fsrs_state=state.get("fsrs_state"),
            )
        for r in c.get("reviews") or []:
            review_repo.import_review(
                qid,
                r["ts"],
                r["result"],
                user_answer=r.get("user_answer") or "",
                grader_notes=r.get("grader_notes") or "",
            )
        queue = c.get("queue")
        if queue:
            trivia_repo.import_entry(
                qid,
                int(queue["position"]),
                last_answered_at=queue.get("last_answered_at"),
                last_answered_correctly=queue.get("last_answered_correctly"),
            )
    return deck_id


# ---- an .apkg source, written the way Anki writes one ---------------------


def build_apkg_source(spec: dict) -> bytes:
    """A minimal but real `.apkg` carrying only what the importer reads:
    a `notes` table with `id` and `flds`. Committing a third-party deck
    is not an option, so every corpus is generated."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            "CREATE TABLE notes (id INTEGER PRIMARY KEY, guid TEXT, mid INTEGER, flds TEXT, sfld TEXT)"
        )
        for n in spec["notes"]:
            conn.execute(
                "INSERT INTO notes (id, guid, mid, flds, sfld) VALUES (?, ?, ?, ?, ?)",
                (n["id"], f"guid-{n['id']}", 1, n["flds"], n["flds"].split("\x1f")[0][:200]),
            )
        conn.commit()
        collection = conn.serialize()
    finally:
        conn.close()

    buf = io.BytesIO()
    stamp = (1980, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        info = zipfile.ZipInfo(filename=spec["collection"], date_time=stamp)
        info.compress_type = zipfile.ZIP_STORED
        zf.writestr(info, collection)
        info = zipfile.ZipInfo(filename="media", date_time=stamp)
        info.compress_type = zipfile.ZIP_STORED
        zf.writestr(info, "{}")
    return buf.getvalue()


# ---- the canonical .apkg dump ---------------------------------------------


def dump_apkg(blob: bytes) -> dict:
    """Entry names in order, the media map, and every collection table as
    `SELECT *` in id order. `csum` is dropped: see the module docstring."""
    zf = zipfile.ZipFile(io.BytesIO(blob))
    names = zf.namelist()
    collection_name = next(n for n in ("collection.anki21", "collection.anki2") if n in names)

    conn = sqlite3.connect(":memory:")
    try:
        conn.deserialize(zf.read(collection_name))
        conn.row_factory = sqlite3.Row
        tables: dict[str, Any] = {}
        for table in DUMPED_TABLES:
            try:
                rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            except sqlite3.OperationalError:
                continue
            dicts = [dict(r) for r in rows]
            if table == "notes":
                for d in dicts:
                    for column in EXCLUDED_NOTE_COLUMNS:
                        d.pop(column, None)
                dicts.sort(key=lambda d: d["id"])
            elif table == "col":
                for d in dicts:
                    for column in COL_JSON_COLUMNS:
                        if column in d:
                            d[column] = json.loads(d[column])
            elif "id" in (dicts[0] if dicts else {}):
                dicts.sort(key=lambda d: d["id"])
            tables[table] = dicts
    finally:
        conn.close()

    return {
        "entries": names,
        "media": zf.read("media").decode("utf-8"),
        "tables": tables,
    }


# ---- the import direction --------------------------------------------------


def deck_rows(user: str, deck_name: str) -> dict:
    """Every row an import produced, keyed so an id sequence that differs
    between implementations does not read as a mismatch."""
    from prep.infrastructure.db import cursor

    with cursor() as c:
        deck = c.execute(
            "SELECT id, name, deck_type, context_prompt, notification_interval_minutes, "
            "trivia_session_size, desired_retention FROM decks WHERE user_id = ? AND name = ?",
            (user, deck_name),
        ).fetchone()
        if deck is None:
            return {"deck": None, "cards": [], "state": [], "reviews": [], "queue": []}
        deck_id = deck["id"]
        questions = c.execute(
            "SELECT id, type, topic, prompt, answer, choices, rubric, skeleton, language, "
            "answer_regex, explanation, suspended FROM questions WHERE deck_id = ? ORDER BY id",
            (deck_id,),
        ).fetchall()
        by_id = {q["id"]: q["prompt"] for q in questions}
        cards = c.execute(
            "SELECT c.question_id, c.step, c.next_due, c.last_review, c.stability, c.difficulty, "
            "c.fsrs_state FROM cards c JOIN questions q ON q.id = c.question_id "
            "WHERE q.deck_id = ? ORDER BY c.question_id",
            (deck_id,),
        ).fetchall()
        reviews = c.execute(
            "SELECT r.question_id, r.ts, r.result, r.user_answer, r.grader_notes FROM reviews r "
            "JOIN questions q ON q.id = r.question_id WHERE q.deck_id = ? ORDER BY r.id",
            (deck_id,),
        ).fetchall()
        queue = c.execute(
            "SELECT t.question_id, t.queue_position, t.last_answered_at, t.last_answered_correctly "
            "FROM trivia_queue t JOIN questions q ON q.id = t.question_id "
            "WHERE q.deck_id = ? ORDER BY t.queue_position",
            (deck_id,),
        ).fetchall()

    def keyed(rows, id_column: str) -> list[dict]:
        out = []
        for r in rows:
            d = dict(r)
            d["prompt"] = by_id.get(d.pop(id_column))
            out.append(d)
        return out

    return {
        "deck": {k: v for k, v in dict(deck).items() if k != "id"},
        "cards": [{k: v for k, v in dict(q).items() if k != "id"} for q in questions],
        "state": keyed(cards, "question_id"),
        "reviews": keyed(reviews, "question_id"),
        "queue": keyed(queue, "question_id"),
    }


def outcome_dict(outcome) -> dict:
    from dataclasses import asdict

    return asdict(outcome)


# ---- extraction ------------------------------------------------------------


def extract() -> dict[str, bytes]:
    """Every golden as bytes; text files are UTF-8 encoded here so one
    writer handles the whole corpus."""
    from prep.decks.anki import apkg_to_deck
    from prep.decks.anki_export import deck_to_apkg
    from prep.decks.archive import deck_to_prepdeck, prepdeck_to_deck
    from prep.decks.io import csv_to_deck, deck_to_csv
    from prep.decks.repo import DeckRepo, QuestionRepo

    spec = load_profiles()
    files: dict[str, bytes] = {"profiles.json": PROFILES_FILE.read_bytes()}

    for profile in spec["profiles"]:
        name = profile["name"]
        with scratch_app() as h:
            h.seed(PARITY_USER, "empty")
            deck_id = build_deck(PARITY_USER, profile)

            csv_text = deck_to_csv(PARITY_USER, deck_id)
            prepdeck = deck_to_prepdeck(PARITY_USER, deck_id)
            apkg = deck_to_apkg(PARITY_USER, deck_id, profile["deck"]["name"])

            files[f"{name}.csv"] = csv_text.encode("utf-8")
            files[f"{name}.prepdeck"] = prepdeck
            files[f"{name}.apkg.dump.json"] = _json_bytes(dump_apkg(apkg))

            # Import direction: each artefact read back into a fresh deck.
            imports: dict[str, Any] = {}
            csv_outcome = csv_to_deck(
                PARITY_USER,
                "from-csv",
                csv_text,
                deck_repo=DeckRepo(),
                question_repo=QuestionRepo(),
            )
            imports["csv"] = {
                "outcome": outcome_dict(csv_outcome),
                "rows": deck_rows(PARITY_USER, "from-csv"),
            }

            prepdeck_outcome = prepdeck_to_deck(
                PARITY_USER,
                "from-prepdeck",
                prepdeck,
                deck_repo=DeckRepo(),
                question_repo=QuestionRepo(),
            )
            imports["prepdeck"] = {
                "outcome": outcome_dict(prepdeck_outcome),
                "rows": deck_rows(PARITY_USER, "from-prepdeck"),
            }

            apkg_outcome = apkg_to_deck(
                PARITY_USER, "from-apkg", apkg, deck_repo=DeckRepo(), question_repo=QuestionRepo()
            )
            imports["apkg"] = {
                "outcome": outcome_dict(apkg_outcome),
                "rows": deck_rows(PARITY_USER, "from-apkg"),
            }
            files[f"{name}.import.json"] = _json_bytes(imports)

    for source in spec["apkg_sources"]:
        name = source["name"]
        blob = build_apkg_source(source)
        files[f"{name}.apkg"] = blob
        with scratch_app() as h:
            h.seed(PARITY_USER, "empty")
            outcome = apkg_to_deck(
                PARITY_USER, "from-apkg", blob, deck_repo=DeckRepo(), question_repo=QuestionRepo()
            )
            files[f"{name}.import.json"] = _json_bytes(
                {
                    "apkg": {
                        "outcome": outcome_dict(outcome),
                        "rows": deck_rows(PARITY_USER, "from-apkg"),
                    }
                }
            )

    return files


def _json_bytes(obj: object) -> bytes:
    return (json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def write() -> Path:
    files = extract()
    if GOLDENS.exists():
        shutil.rmtree(GOLDENS)
    GOLDENS.mkdir(parents=True)
    for rel, body in files.items():
        (GOLDENS / rel).write_bytes(body)
    return GOLDENS


if __name__ == "__main__":
    with pin_clock():
        root = write()
    print(f"{NAME}: {len(list(root.iterdir()))} files -> {root}")
