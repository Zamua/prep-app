"""`.prepdeck` archive — comprehensive deck export/import.

A `.prepdeck` is a zip with three or four entries:

```
deck.prepdeck/
├── meta.json           name, deck_type, exporter version, format_version, …
├── cards.csv           question content + per-card FSRS state (SRS only)
├── reviews.csv         per-card review log (right/wrong/timestamp), SRS only
└── trivia_queue.csv    per-card queue state (trivia only)
```

The plain `.csv` format `prep.decks.io` handles continues to exist for
the "Anki-compat / no history" use case. `.prepdeck` carries everything
needed to reconstruct a deck (FSRS state, reviews log, trivia queue).

## DDD shape

This module is the codec — it orchestrates between contexts but does
NOT touch sqlite directly. SRS state + reviews come from
`prep.study.repo.ReviewRepo` (the study context owns reviews + cards
state). Trivia queue comes from `prep.trivia.repo.TriviaQueueRepo`.
Question content comes from `prep.decks.repo.QuestionRepo` via the
existing `_questions_for_export` helper.

## Why a custom extension instead of `.zip`

`.prepdeck` self-documents the intent — anyone with the file knows
it's a prep export, not a generic archive — and the import route can
sniff the extension to pick the right codec without parsing the body
first. The MIME stays `application/zip` because that IS the wire
format; users who rename the file to `.zip` can still inspect the
contents in any unzip tool.

## Format versioning + backward compatibility

`meta.json` carries `format_version` (integer, starts at 1). Importer
rules:

- `version <= current`: read it. If the writer ran a strictly older
  version, the per-section parser tolerates missing/fewer fields
  (additive evolution only).
- `version > current`: refuse with a clear error.

The exporter NEVER drops fields. New columns get APPENDED to each
CSV; new keys go INTO `meta.json` next to existing ones. This way a
v2 importer can read v1 archives and a future v3 importer can still
read v2.

## Why prompt is the join key across cards/reviews/trivia_queue

reviews.csv and trivia_queue.csv reference cards. We can't use
the source-side `question_id` because reimporting assigns fresh ids.
We use `prompt` as the natural key — the CSV importer already rejects
duplicate prompts per deck, and the join happens once at import time
so post-import edits don't affect history fidelity.
"""

from __future__ import annotations

import csv
import datetime
import io
import json
import re
import zipfile
from dataclasses import dataclass

from prep.decks.entities import NewQuestion, Question, QuestionType
from prep.decks.io import CSV_COLUMNS, _question_to_row, _questions_for_export
from prep.decks.repo import DeckRepo, QuestionRepo

FORMAT_VERSION = 1

# Per-card SRS state columns appended to CSV_COLUMNS in cards.csv.
# NULL-tolerant; a card that's never been reviewed has empty cells.
CARD_STATE_COLUMNS = (
    "step",
    "next_due",
    "last_review",
    "stability",
    "difficulty",
    "fsrs_state",
)

# trivia_queue.csv columns. `queue_position` defines card order on
# reimport; the rest is per-card progress.
TRIVIA_QUEUE_COLUMNS = (
    "queue_position",
    "last_answered_at",
    "last_answered_correctly",
)

# reviews.csv columns. `prompt` joins back to cards on the destination
# side; the rest is the audit-log shape from `reviews`.
REVIEW_COLUMNS = (
    "prompt",
    "ts",
    "result",
    "user_answer",
    "grader_notes",
)


# ---- export -------------------------------------------------------------


def _build_meta(deck_repo: DeckRepo, user_id: str, deck_id: int) -> dict:
    """Read deck-level config via the repo and shape it into the
    meta.json payload. Exporter version is embedded so support requests
    can tell which build wrote a given archive."""
    # Cheap one-row lookup; the existing DeckRepo helpers don't return
    # the full set together (each is a focused getter), so we use them
    # in concert rather than reaching past them.
    deck_type = deck_repo.get_type(user_id, deck_id)
    if deck_type is None:
        raise ValueError(f"deck {deck_id} not found for user {user_id!r}")
    meta_obj = deck_repo.get_meta(user_id, deck_id)
    name = deck_repo.find_name(user_id, deck_id)
    desired_retention = deck_repo.get_desired_retention(user_id, deck_id)

    return {
        "format_version": FORMAT_VERSION,
        "exported_at": datetime.datetime.now(datetime.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "deck": {
            "name": name,
            "deck_type": deck_type.value,
            "context_prompt": meta_obj.context_prompt or None,
            "notification_interval_minutes": meta_obj.interval_minutes,
            "trivia_session_size": int(meta_obj.session_size or 3),
            "desired_retention": desired_retention,
        },
    }


def _cards_csv_for_deck(user_id: str, deck_id: int, review_repo) -> tuple[str, list[Question]]:
    """Build cards.csv. Returns (csv_text, questions) so callers can
    reuse the question list for the reviews / queue sections without
    a second fetch."""
    questions = _questions_for_export(user_id, deck_id)
    state_by_prompt: dict[str, dict] = {
        row["prompt"]: row for row in review_repo.list_card_state_for_deck(user_id, deck_id)
    }

    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=list(CSV_COLUMNS) + list(CARD_STATE_COLUMNS),
        extrasaction="ignore",
    )
    writer.writeheader()
    for q in questions:
        row = _question_to_row(q)
        s = state_by_prompt.get(q.prompt) or {}
        row.update(
            {
                "step": "" if s.get("step") is None else str(s["step"]),
                "next_due": s.get("next_due") or "",
                "last_review": s.get("last_review") or "",
                "stability": "" if s.get("stability") is None else f"{s['stability']:.6g}",
                "difficulty": "" if s.get("difficulty") is None else f"{s['difficulty']:.6g}",
                "fsrs_state": "" if s.get("fsrs_state") is None else str(s["fsrs_state"]),
            }
        )
        writer.writerow(row)
    return buf.getvalue(), questions


def _reviews_csv_for_deck(user_id: str, deck_id: int, review_repo) -> str:
    """Build reviews.csv via ReviewRepo."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(REVIEW_COLUMNS), extrasaction="ignore")
    writer.writeheader()
    for r in review_repo.list_reviews_for_deck(user_id, deck_id):
        writer.writerow(
            {
                "prompt": r["prompt"],
                "ts": r["ts"],
                "result": r["result"],
                "user_answer": r["user_answer"] or "",
                "grader_notes": r["grader_notes"] or "",
            }
        )
    return buf.getvalue()


def _trivia_queue_csv_for_deck(deck_id: int, trivia_repo) -> str:
    """Build trivia_queue.csv via TriviaQueueRepo."""
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf, fieldnames=["prompt", *TRIVIA_QUEUE_COLUMNS], extrasaction="ignore"
    )
    writer.writeheader()
    for r in trivia_repo.list_queue_for_deck(deck_id):
        writer.writerow(
            {
                "prompt": r["prompt"],
                "queue_position": str(r["queue_position"]),
                "last_answered_at": r["last_answered_at"] or "",
                "last_answered_correctly": (
                    ""
                    if r["last_answered_correctly"] is None
                    else str(int(r["last_answered_correctly"]))
                ),
            }
        )
    return buf.getvalue()


def deck_to_prepdeck(user_id: str, deck_id: int) -> bytes:
    """Build a `.prepdeck` archive (zip bytes) for `deck_id`.

    Layout:
      - meta.json          deck-level config + format version
      - cards.csv          per-card content + FSRS state
      - reviews.csv        per-card review log
      - trivia_queue.csv   per-card queue state (only emitted for trivia)

    Compression is stored (no zlib) with a fixed mtime so two
    consecutive exports of the same deck produce byte-identical zips
    — useful for round-trip tests."""
    from prep.study.repo import ReviewRepo
    from prep.trivia.repo import TriviaQueueRepo

    deck_repo = DeckRepo()
    review_repo = ReviewRepo()
    trivia_repo = TriviaQueueRepo()

    meta = _build_meta(deck_repo, user_id, deck_id)
    cards_csv, _ = _cards_csv_for_deck(user_id, deck_id, review_repo)
    reviews_csv = _reviews_csv_for_deck(user_id, deck_id, review_repo)
    is_trivia = meta["deck"]["deck_type"] == "trivia"

    buf = io.BytesIO()
    DETERMINISTIC_DATE = (1980, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for name, body in [
            ("meta.json", json.dumps(meta, indent=2, sort_keys=True) + "\n"),
            ("cards.csv", cards_csv),
            ("reviews.csv", reviews_csv),
        ]:
            info = zipfile.ZipInfo(filename=name, date_time=DETERMINISTIC_DATE)
            info.compress_type = zipfile.ZIP_STORED
            zf.writestr(info, body)
        if is_trivia:
            trivia_csv = _trivia_queue_csv_for_deck(deck_id, trivia_repo)
            info = zipfile.ZipInfo(filename="trivia_queue.csv", date_time=DETERMINISTIC_DATE)
            info.compress_type = zipfile.ZIP_STORED
            zf.writestr(info, trivia_csv)
    return buf.getvalue()


# ---- import -------------------------------------------------------------


@dataclass(frozen=True)
class PrepdeckImportOutcome:
    """Result of a `.prepdeck` import. Mirrors prep.decks.io.ImportOutcome
    with extra counters for the review log + queue rows restored."""

    deck_id: int
    deck_name: str
    inserted: int
    skipped_duplicates: int
    reviews_inserted: int
    queue_rows_inserted: int
    errors: list[str]


_VALID_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,29}$")


def _fail(deck_name: str, error: str) -> PrepdeckImportOutcome:
    return PrepdeckImportOutcome(
        deck_id=0,
        deck_name=deck_name,
        inserted=0,
        skipped_duplicates=0,
        reviews_inserted=0,
        queue_rows_inserted=0,
        errors=[error],
    )


def prepdeck_to_deck(
    user_id: str,
    deck_name: str,
    zip_bytes: bytes,
    *,
    deck_repo: DeckRepo,
    question_repo: QuestionRepo,
) -> PrepdeckImportOutcome:
    """Import a `.prepdeck` archive into a fresh deck named `deck_name`.

    Refuses if `deck_name` already exists — full-state restore into a
    deck with prior content would mean awkward merges of FSRS state
    and reviews. The caller picks a new name. (The plain `.csv`
    importer's get-or-create behavior makes sense there because the
    semantics are "append cards"; here the semantics are "restore deck.")
    """
    from prep.study.repo import ReviewRepo
    from prep.trivia.repo import TriviaQueueRepo

    review_repo = ReviewRepo()
    trivia_repo = TriviaQueueRepo()
    errors: list[str] = []

    if not _VALID_NAME.match(deck_name):
        return _fail(
            deck_name,
            f"invalid deck name {deck_name!r}; must match {_VALID_NAME.pattern}",
        )
    if deck_repo.find_id(user_id, deck_name) is not None:
        return _fail(
            deck_name,
            f"deck {deck_name!r} already exists. Pick a fresh name — "
            ".prepdeck imports restore full state, which would merge "
            "awkwardly with existing cards.",
        )

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as e:
        return _fail(deck_name, f"not a valid zip: {e}")

    names = set(zf.namelist())
    required = {"meta.json", "cards.csv", "reviews.csv"}
    missing = required - names
    if missing:
        return _fail(deck_name, f"archive is missing required entries: {sorted(missing)}")

    try:
        meta = json.loads(zf.read("meta.json"))
    except (json.JSONDecodeError, KeyError) as e:
        return _fail(deck_name, f"meta.json is not valid JSON: {e}")

    version = int(meta.get("format_version") or 0)
    if version > FORMAT_VERSION:
        return _fail(
            deck_name,
            f".prepdeck format_version {version} is newer than this prep "
            f"build supports ({FORMAT_VERSION}). Upgrade prep or re-export "
            "from the source deploy at an older format.",
        )
    if version < 1:
        return _fail(deck_name, f".prepdeck format_version {version} is unknown; must be ≥ 1")

    deck_meta = meta.get("deck") or {}
    declared_type = (deck_meta.get("deck_type") or "srs").lower()

    # Create the deck.
    if declared_type == "trivia":
        interval = int(deck_meta.get("notification_interval_minutes") or 30)
        topic = deck_meta.get("context_prompt") or ""
        deck_id = deck_repo.create_trivia(
            user_id, deck_name, topic=topic, interval_minutes=interval
        )
        if deck_meta.get("trivia_session_size"):
            try:
                deck_repo.set_trivia_session_size(
                    user_id, deck_id, int(deck_meta["trivia_session_size"])
                )
            except (TypeError, ValueError):
                pass
    else:
        deck_id = deck_repo.create(
            user_id, deck_name, context_prompt=deck_meta.get("context_prompt")
        )

    if deck_meta.get("desired_retention") is not None:
        try:
            deck_repo.set_desired_retention(user_id, deck_id, float(deck_meta["desired_retention"]))
        except (TypeError, ValueError):
            pass

    # ---- cards.csv ----
    inserted, skipped_duplicates, qid_by_prompt = _import_cards(
        user_id,
        deck_id,
        declared_type,
        zf.read("cards.csv").decode("utf-8"),
        question_repo=question_repo,
        review_repo=review_repo,
        errors=errors,
    )

    # ---- reviews.csv ----
    reviews_inserted = _import_reviews(
        zf.read("reviews.csv").decode("utf-8"),
        qid_by_prompt=qid_by_prompt,
        review_repo=review_repo,
        errors=errors,
    )

    # ---- trivia_queue.csv ----
    queue_rows_inserted = 0
    if declared_type == "trivia":
        if "trivia_queue.csv" in names:
            queue_rows_inserted = _import_trivia_queue(
                zf.read("trivia_queue.csv").decode("utf-8"),
                qid_by_prompt=qid_by_prompt,
                trivia_repo=trivia_repo,
                errors=errors,
            )
        else:
            # Older archives without the trivia_queue section: rebuild
            # the queue from cards.csv order so the deck is at least
            # studyable. Logged so the user knows progress was lost.
            for qid in qid_by_prompt.values():
                trivia_repo.append_card(qid, deck_id)
                queue_rows_inserted += 1
            errors.append(
                "trivia_queue.csv was missing — queue rebuilt from cards.csv order; "
                "per-card answered state was lost."
            )

    return PrepdeckImportOutcome(
        deck_id=deck_id,
        deck_name=deck_name,
        inserted=inserted,
        skipped_duplicates=skipped_duplicates,
        reviews_inserted=reviews_inserted,
        queue_rows_inserted=queue_rows_inserted,
        errors=errors,
    )


def _import_cards(
    user_id: str,
    deck_id: int,
    declared_type: str,
    csv_text: str,
    *,
    question_repo: QuestionRepo,
    review_repo,
    errors: list[str],
) -> tuple[int, int, dict[str, int]]:
    """Insert every card row, then for SRS decks call back into
    ReviewRepo.restore_card_state to overwrite the post-insert
    defaults with the source's FSRS state. Returns
    (inserted, skipped_duplicates, qid_by_prompt) for the caller to
    feed into the reviews/queue importers."""
    reader = csv.DictReader(io.StringIO(csv_text))
    inserted = 0
    skipped_duplicates = 0
    qid_by_prompt: dict[str, int] = {}
    for i, row in enumerate(reader, start=2):
        prompt = (row.get("prompt") or "").strip()
        if not prompt:
            errors.append(f"cards.csv row {i}: missing prompt")
            continue
        if prompt in qid_by_prompt:
            skipped_duplicates += 1
            continue
        try:
            qtype = QuestionType((row.get("type") or "short").strip().lower())
        except ValueError:
            errors.append(f"cards.csv row {i}: unknown type {row.get('type')!r}")
            continue
        answer = (row.get("answer") or "").strip()
        if not answer:
            errors.append(f"cards.csv row {i}: missing answer")
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
        except Exception as e:  # noqa: BLE001
            errors.append(f"cards.csv row {i}: {e}")
            continue
        try:
            qid = question_repo.add(user_id, deck_id, new)
        except Exception as e:  # noqa: BLE001
            errors.append(f"cards.csv row {i}: write failed — {e}")
            continue
        qid_by_prompt[prompt] = qid
        inserted += 1

        if declared_type == "srs":
            _restore_one_card_state(qid, row, review_repo, errors, i)
    return inserted, skipped_duplicates, qid_by_prompt


def _restore_one_card_state(
    qid: int, row: dict, review_repo, errors: list[str], row_num: int
) -> None:
    """Pull the state columns out of a cards.csv row and hand them to
    ReviewRepo.restore_card_state. Empty cells stay at the post-insert
    defaults."""

    def _maybe_int(name: str) -> int | None:
        v = (row.get(name) or "").strip()
        if not v:
            return None
        try:
            return int(v)
        except ValueError:
            errors.append(f"cards.csv row {row_num}: bad {name}={v!r}")
            return None

    def _maybe_float(name: str) -> float | None:
        v = (row.get(name) or "").strip()
        if not v:
            return None
        try:
            return float(v)
        except ValueError:
            errors.append(f"cards.csv row {row_num}: bad {name}={v!r}")
            return None

    def _maybe_text(name: str) -> str | None:
        v = (row.get(name) or "").strip()
        return v or None

    review_repo.restore_card_state(
        qid,
        step=_maybe_int("step"),
        next_due=_maybe_text("next_due"),
        last_review=_maybe_text("last_review"),
        stability=_maybe_float("stability"),
        difficulty=_maybe_float("difficulty"),
        fsrs_state=_maybe_int("fsrs_state"),
    )


def _import_reviews(
    csv_text: str,
    *,
    qid_by_prompt: dict[str, int],
    review_repo,
    errors: list[str],
) -> int:
    """Replay reviews.csv into ReviewRepo.import_review (bypasses the
    scheduler — the cards-level state is already restored)."""
    reader = csv.DictReader(io.StringIO(csv_text))
    inserted = 0
    for i, row in enumerate(reader, start=2):
        prompt = (row.get("prompt") or "").strip()
        if not prompt:
            errors.append(f"reviews.csv row {i}: missing prompt")
            continue
        qid = qid_by_prompt.get(prompt)
        if qid is None:
            errors.append(f"reviews.csv row {i}: prompt {prompt[:40]!r} not found in deck")
            continue
        result = (row.get("result") or "").strip().lower()
        if result not in ("right", "wrong"):
            errors.append(f"reviews.csv row {i}: bad result {result!r}")
            continue
        try:
            review_repo.import_review(
                qid,
                row.get("ts") or "",
                result,
                user_answer=row.get("user_answer") or "",
                grader_notes=row.get("grader_notes") or "",
            )
            inserted += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"reviews.csv row {i}: write failed — {e}")
    return inserted


def _import_trivia_queue(
    csv_text: str,
    *,
    qid_by_prompt: dict[str, int],
    trivia_repo,
    errors: list[str],
) -> int:
    """Replay trivia_queue.csv into TriviaQueueRepo.import_entry. Sorts
    by queue_position so the rotation order is preserved."""
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    rows.sort(key=lambda r: int((r.get("queue_position") or "0").strip() or 0))
    inserted = 0
    for i, row in enumerate(rows, start=2):
        prompt = (row.get("prompt") or "").strip()
        qid = qid_by_prompt.get(prompt)
        if qid is None:
            errors.append(f"trivia_queue.csv row {i}: prompt {prompt[:40]!r} not in deck")
            continue
        try:
            pos = int((row.get("queue_position") or "0").strip() or 0)
        except ValueError:
            errors.append(f"trivia_queue.csv row {i}: bad queue_position")
            continue
        lac_raw = (row.get("last_answered_correctly") or "").strip()
        last_answered_correctly: int | None
        if lac_raw == "":
            last_answered_correctly = None
        else:
            try:
                last_answered_correctly = int(lac_raw)
            except ValueError:
                errors.append(f"trivia_queue.csv row {i}: bad last_answered_correctly={lac_raw!r}")
                continue
        try:
            trivia_repo.import_entry(
                qid,
                pos,
                last_answered_at=row.get("last_answered_at") or None,
                last_answered_correctly=last_answered_correctly,
            )
            inserted += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"trivia_queue.csv row {i}: write failed — {e}")
    return inserted
