"""CSV export + import tests for the decks bounded context."""

from __future__ import annotations

import csv as csvmod
import io

from prep.decks.entities import NewQuestion, QuestionType
from prep.decks.io import CSV_COLUMNS, csv_to_deck, deck_to_csv
from prep.decks.repo import DeckRepo, QuestionRepo


def _seed_deck(initialized_db: str, name: str = "geo") -> tuple[int, list[int]]:
    """Create a deck with three short cards, return (deck_id, qids)."""
    deck_repo = DeckRepo()
    qr = QuestionRepo()
    deck_id = deck_repo.create(initialized_db, name)
    qids = []
    for prompt, ans, regex in [
        ("Capital of France?", "Paris", "paris"),
        ("Capital of Japan?", "Tokyo", "tokyo"),
        ("Capital of Brazil?", "Brasília", "bras[ií]lia|brasilia"),
    ]:
        qid = qr.add(
            initialized_db,
            deck_id,
            NewQuestion(
                type=QuestionType.SHORT,
                prompt=prompt,
                answer=ans,
                answer_regex=regex,
                topic="world capitals",
            ),
        )
        qids.append(qid)
    return deck_id, qids


def test_csv_export_round_trips_via_python_csv(initialized_db: str):
    """deck_to_csv emits a header + one row per question, parseable by
    the stdlib csv reader."""
    deck_id, _ = _seed_deck(initialized_db, "round-trip")
    body = deck_to_csv(initialized_db, deck_id)

    reader = csvmod.DictReader(io.StringIO(body))
    assert tuple(reader.fieldnames) == CSV_COLUMNS
    rows = list(reader)
    assert len(rows) == 3
    assert {r["prompt"] for r in rows} == {
        "Capital of France?",
        "Capital of Japan?",
        "Capital of Brazil?",
    }
    paris = next(r for r in rows if r["prompt"] == "Capital of France?")
    assert paris["answer"] == "Paris"
    assert paris["answer_regex"] == "paris"
    assert paris["type"] == "short"
    assert paris["topic"] == "world capitals"


def test_csv_export_handles_multiline_choices(initialized_db: str):
    """MCQ choices live as a JSON array internally but should be
    newline-joined in the CSV so they round-trip cleanly through
    Anki / spreadsheet apps."""
    deck_repo = DeckRepo()
    qr = QuestionRepo()
    deck_id = deck_repo.create(initialized_db, "mcq-deck")
    qr.add(
        initialized_db,
        deck_id,
        NewQuestion(
            type=QuestionType.MCQ,
            prompt="Pick one.",
            answer="alpha",
            choices=["alpha", "beta", "gamma"],
        ),
    )
    body = deck_to_csv(initialized_db, deck_id)
    rows = list(csvmod.DictReader(io.StringIO(body)))
    assert len(rows) == 1
    assert rows[0]["choices"] == "alpha\nbeta\ngamma"


def test_csv_import_creates_deck_and_inserts(initialized_db: str):
    """Happy path: header + 2 rows lands as a new deck with 2
    questions."""
    csv_text = (
        "type,topic,prompt,answer,answer_regex\n"
        "short,europe,Capital of Spain?,Madrid,madrid\n"
        "short,europe,Capital of Italy?,Rome,rome|roma\n"
    )
    out = csv_to_deck(
        initialized_db,
        "europe-caps",
        csv_text,
        deck_repo=DeckRepo(),
        question_repo=QuestionRepo(),
    )
    assert out.inserted == 2
    assert out.skipped_duplicates == 0
    assert out.errors == []
    cards = QuestionRepo().list_in_deck(initialized_db, out.deck_id)
    assert {c.prompt for c in cards} == {"Capital of Spain?", "Capital of Italy?"}


def test_csv_import_dedups_by_prompt(initialized_db: str):
    """Re-importing a row whose prompt already exists in the target
    deck silently increments skipped_duplicates."""
    csv_text = (
        "type,prompt,answer\n"
        "short,Capital of France?,Paris\n"  # already in deck
        "short,Capital of Spain?,Madrid\n"  # new
    )
    _seed_deck(initialized_db, "geo")
    out = csv_to_deck(
        initialized_db,
        "geo",
        csv_text,
        deck_repo=DeckRepo(),
        question_repo=QuestionRepo(),
    )
    assert out.inserted == 1
    assert out.skipped_duplicates == 1
    assert out.errors == []


def test_csv_import_collects_row_errors_without_aborting(initialized_db: str):
    """A single bad row (missing answer) should be recorded as an
    error but not block the other rows from inserting."""
    csv_text = (
        "type,prompt,answer\n"
        "short,Q1,A1\n"
        "short,Q2,\n"  # missing answer → error
        "bogus-type,Q3,A3\n"  # unknown type → error
        "short,Q4,A4\n"
    )
    out = csv_to_deck(
        initialized_db,
        "mixed",
        csv_text,
        deck_repo=DeckRepo(),
        question_repo=QuestionRepo(),
    )
    assert out.inserted == 2
    assert len(out.errors) == 2
    assert "row 3" in out.errors[0]
    assert "row 4" in out.errors[1]


def test_csv_export_and_reimport_yields_identical_cards(initialized_db: str):
    """End-to-end: export a deck, reimport into a fresh deck, the
    cards' user-visible fields match."""
    deck_id, _ = _seed_deck(initialized_db, "src")
    body = deck_to_csv(initialized_db, deck_id)

    out = csv_to_deck(
        initialized_db,
        "dst",
        body,
        deck_repo=DeckRepo(),
        question_repo=QuestionRepo(),
    )
    assert out.inserted == 3
    assert out.errors == []

    src = QuestionRepo().list_in_deck(initialized_db, deck_id)
    dst = QuestionRepo().list_in_deck(initialized_db, out.deck_id)
    src_by_prompt = {q.prompt: q for q in src}
    dst_by_prompt = {q.prompt: q for q in dst}
    assert set(src_by_prompt) == set(dst_by_prompt)
    for prompt, s in src_by_prompt.items():
        d = dst_by_prompt[prompt]
        assert (s.answer, s.answer_regex, s.topic, s.type) == (
            d.answer,
            d.answer_regex,
            d.topic,
            d.type,
        )


def test_csv_import_empty_csv_returns_zero_inserts(initialized_db: str):
    out = csv_to_deck(
        initialized_db,
        "empty",
        "",
        deck_repo=DeckRepo(),
        question_repo=QuestionRepo(),
    )
    assert out.inserted == 0
    assert out.errors == ["CSV has no header row"]


# ---- HTTP route smoke tests --------------------------------------------


def test_export_csv_route_returns_attachment(client, initialized_db: str):
    """GET /deck/<name>/export.csv → 200 + text/csv with the right
    content-disposition + the deck's rows in the body."""
    _seed_deck(initialized_db, "geo")
    r = client.get("/deck/geo/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="geo.csv"' in r.headers["content-disposition"]
    assert "Capital of France?" in r.text
    assert "Paris" in r.text


def test_export_csv_route_404s_for_missing_deck(client, initialized_db: str):
    r = client.get("/deck/no-such-deck/export.csv")
    assert r.status_code == 404


def test_import_csv_route_renders_outcome(client, initialized_db: str):
    """POST /decks/import-csv with a multipart body lands an outcome
    block in the rendered page."""
    csv_bytes = b"type,prompt,answer\n" b"short,Q1,A1\n" b"short,Q2,A2\n"
    r = client.post(
        "/decks/import-csv",
        data={"name": "via-http"},
        files={"file": ("x.csv", csv_bytes, "text/csv")},
    )
    assert r.status_code == 200
    # The outcome block renders with `<strong>2</strong> cards added.`
    # so a bare substring check on raw HTML is brittle — verify via
    # the DB instead.
    cards = QuestionRepo().list_in_deck(
        initialized_db, DeckRepo().find_id(initialized_db, "via-http")
    )
    assert {c.prompt for c in cards} == {"Q1", "Q2"}
    # And the outcome panel did render (the eyebrow heading carries
    # the deck name in inline code).
    assert "via-http" in r.text
