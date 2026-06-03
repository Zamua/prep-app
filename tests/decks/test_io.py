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


def _seed_comprehensive_deck(initialized_db: str, name: str = "round-trip-everything") -> int:
    """Seed a deck with one card of each type, every optional field
    populated where it's structurally meaningful for that type.

    Coverage target:
      - SHORT:  topic, rubric, explanation, answer_regex
      - MCQ:    topic, rubric, explanation, choices (3 entries)
      - MULTI:  topic, rubric, explanation, choices (4), answer as
                JSON-encoded list of two correct choices
      - CODE:   topic, rubric, explanation, skeleton, language

    Anything CSV-exportable that's NOT covered here would slip through
    the round-trip test below, so the goal is breadth over depth.
    """
    import json

    deck_repo = DeckRepo()
    qr = QuestionRepo()
    deck_id = deck_repo.create(initialized_db, name)

    qr.add(
        initialized_db,
        deck_id,
        NewQuestion(
            type=QuestionType.SHORT,
            topic="biology",
            prompt="What organelle generates ATP?",
            answer="mitochondria",
            answer_regex="mitochondri[ao]n?",
            rubric="must name the organelle; spelling tolerant",
            explanation="The mitochondrion is the site of oxidative phosphorylation.",
        ),
    )
    qr.add(
        initialized_db,
        deck_id,
        NewQuestion(
            type=QuestionType.MCQ,
            topic="geography",
            prompt="Which is the capital of Australia?",
            answer="Canberra",
            choices=["Sydney", "Melbourne", "Canberra", "Perth"],
            rubric="single correct city; Sydney is the largest, not the capital",
            explanation="Common confusion — Canberra was chosen as a compromise between Sydney and Melbourne.",
        ),
    )
    qr.add(
        initialized_db,
        deck_id,
        NewQuestion(
            type=QuestionType.MULTI,
            topic="systems",
            prompt="Which of these are ACID properties?",
            answer=json.dumps(["atomicity", "isolation"]),
            choices=["atomicity", "scaling", "isolation", "latency"],
            rubric="exactly the two ACID properties listed; partial credit not accepted",
            explanation="ACID = atomicity, consistency, isolation, durability — only A and I appear in the choices.",
        ),
    )
    qr.add(
        initialized_db,
        deck_id,
        NewQuestion(
            type=QuestionType.CODE,
            topic="python",
            prompt="Implement a thread-safe counter.",
            answer=(
                "import threading\n"
                "class Counter:\n"
                "    def __init__(self):\n"
                "        self._n = 0\n"
                "        self._lock = threading.Lock()\n"
                "    def increment(self):\n"
                "        with self._lock:\n"
                "            self._n += 1\n"
                "    def value(self):\n"
                "        with self._lock:\n"
                "            return self._n"
            ),
            skeleton=(
                "class Counter:\n"
                "    def __init__(self):\n"
                "        ...\n"
                "    def increment(self):\n"
                "        ...\n"
                "    def value(self):\n"
                "        ..."
            ),
            language="python",
            rubric="counter must be safe under N*K increments from concurrent threads",
            explanation="A lock around both increment and value prevents lost updates and torn reads.",
        ),
    )
    return deck_id


def test_csv_round_trip_preserves_all_fields_all_types(initialized_db: str):
    """End-to-end fidelity check: a deck with one card of EVERY type and
    EVERY optional field populated round-trips through CSV without any
    user-visible drift.

    The earlier `test_csv_export_and_reimport_yields_identical_cards`
    used a uniform short-deck and checked a 4-field subset — that pins
    the basics. This test extends to mcq + multi + code AND asserts
    every column in CSV_COLUMNS round-trips. If CSV gains a new
    column, this test will catch a missed field at the import or
    export end.
    """
    src_deck_id = _seed_comprehensive_deck(initialized_db, "round-trip-src")
    body = deck_to_csv(initialized_db, src_deck_id)

    out = csv_to_deck(
        initialized_db,
        "round-trip-dst",
        body,
        deck_repo=DeckRepo(),
        question_repo=QuestionRepo(),
    )
    assert (
        out.inserted == 4
    ), f"expected 4 cards reimported, got {out.inserted}; errors={out.errors}"
    assert out.errors == []

    # Fetch full Question entities directly so we can diff every column
    # the CSV touches. `list_in_deck` returns a DeckCard projection that
    # drops `explanation`; pull the raw Question shape via the same
    # internal helper the export uses to stay consistent.
    from prep.decks.io import _questions_for_export

    src = _questions_for_export(initialized_db, src_deck_id)
    dst = _questions_for_export(initialized_db, out.deck_id)
    assert len(src) == len(dst) == 4

    # Compare by prompt so we don't depend on insertion-order survival
    # through the import path (the importer respects CSV row order but
    # the assertion shouldn't bake that in).
    src_by_prompt = {q.prompt: q for q in src}
    dst_by_prompt = {q.prompt: q for q in dst}
    assert set(src_by_prompt) == set(dst_by_prompt)

    for prompt, s in src_by_prompt.items():
        d = dst_by_prompt[prompt]
        assert s.type == d.type, f"{prompt}: type drift {s.type} → {d.type}"
        assert s.topic == d.topic, f"{prompt}: topic drift {s.topic!r} → {d.topic!r}"
        assert s.answer == d.answer, f"{prompt}: answer drift"
        assert s.choices == d.choices, f"{prompt}: choices drift {s.choices!r} → {d.choices!r}"
        assert s.rubric == d.rubric, f"{prompt}: rubric drift"
        assert s.skeleton == d.skeleton, f"{prompt}: skeleton drift"
        assert s.language == d.language, f"{prompt}: language drift {s.language!r} → {d.language!r}"
        assert (
            s.answer_regex == d.answer_regex
        ), f"{prompt}: answer_regex drift {s.answer_regex!r} → {d.answer_regex!r}"
        assert s.explanation == d.explanation, f"{prompt}: explanation drift"


def test_csv_double_export_is_byte_identical(initialized_db: str):
    """Export → import → export should yield the same CSV bytes as the
    first export (sorted by id ASC, which is the deterministic order
    `_questions_for_export` uses). Catches subtle re-serialization
    drift like whitespace normalization or list-order changes in
    `choices` that a field-by-field diff might miss."""
    src_deck_id = _seed_comprehensive_deck(initialized_db, "double-export-src")
    first = deck_to_csv(initialized_db, src_deck_id)

    out = csv_to_deck(
        initialized_db,
        "double-export-dst",
        first,
        deck_repo=DeckRepo(),
        question_repo=QuestionRepo(),
    )
    assert out.inserted == 4
    second = deck_to_csv(initialized_db, out.deck_id)
    assert first == second, (
        "second export differs from first — something is mutating in the "
        "import-then-re-export path. Diff:\n"
        f"--- first ---\n{first}\n--- second ---\n{second}"
    )


def _seed_trivia_deck(initialized_db: str, name: str = "trivia-src") -> tuple[int, list[int]]:
    """Seed a trivia deck with three short cards + their queue rows.
    Mirrors what the trivia batch generator would produce in real use.
    Returns (deck_id, queue-ordered-qids)."""
    from prep.trivia.repo import TriviaQueueRepo

    deck_repo = DeckRepo()
    qr = QuestionRepo()
    deck_id = deck_repo.create_trivia(
        initialized_db, name, topic="world capitals", interval_minutes=45
    )
    deck_repo.set_trivia_session_size(initialized_db, deck_id, 5)
    tq = TriviaQueueRepo()
    qids = []
    for prompt, ans in [
        ("What is the capital of France?", "Paris"),
        ("What is the capital of Japan?", "Tokyo"),
        ("What is the capital of Brazil?", "Brasília"),
    ]:
        qid = qr.add(
            initialized_db,
            deck_id,
            NewQuestion(
                type=QuestionType.SHORT,
                topic="capitals",
                prompt=prompt,
                answer=ans,
                explanation=f"{ans} is the political and administrative capital.",
            ),
        )
        tq.append_card(qid, deck_id)
        qids.append(qid)
    return deck_id, qids


def test_csv_trivia_round_trip_preserves_deck_shape_and_queue(initialized_db: str):
    """Trivia decks round-trip with their full shape: deck_type,
    notification_interval_minutes, trivia_session_size, the topic
    prompt, and the trivia_queue order. The CSV preamble carries the
    deck-level state; the importer reconstructs both the deck and
    the queue."""
    from prep.decks.entities import DeckType

    src_deck_id, src_qids = _seed_trivia_deck(initialized_db, "trivia-src")
    body = deck_to_csv(initialized_db, src_deck_id)

    # The preamble announces it as trivia + carries the per-deck config.
    assert "# deck_type: trivia" in body
    assert "# notification_interval_minutes: 45" in body
    assert "# trivia_session_size: 5" in body
    assert "# topic_prompt: world capitals" in body
    # Preamble sits before the CSV header.
    assert body.index("# deck_type") < body.index("type,topic,prompt")

    out = csv_to_deck(
        initialized_db,
        "trivia-dst",
        body,
        deck_repo=DeckRepo(),
        question_repo=QuestionRepo(),
    )
    assert out.inserted == 3, f"errors={out.errors}"
    assert out.errors == []

    # Destination deck has the expected type + per-deck config.
    deck_repo = DeckRepo()
    dst_type = deck_repo.get_type(initialized_db, out.deck_id)
    assert dst_type == DeckType.TRIVIA
    meta = deck_repo.get_meta(initialized_db, out.deck_id)
    assert meta.interval_minutes == 45
    assert meta.session_size == 5
    assert meta.context_prompt == "world capitals"

    # Per-card content survived (using the same _questions_for_export
    # helper to bypass the DeckCard projection's missing-explanation
    # shim).
    from prep.decks.io import _questions_for_export

    src = _questions_for_export(initialized_db, src_deck_id)
    dst = _questions_for_export(initialized_db, out.deck_id)
    assert len(src) == len(dst) == 3
    src_by_prompt = {q.prompt: q for q in src}
    dst_by_prompt = {q.prompt: q for q in dst}
    assert set(src_by_prompt) == set(dst_by_prompt)
    for prompt, s in src_by_prompt.items():
        d = dst_by_prompt[prompt]
        assert s.answer == d.answer
        assert s.topic == d.topic
        assert s.explanation == d.explanation

    # trivia_queue rebuilt: every imported question has a queue row,
    # and the queue order matches the CSV row order (which is the
    # original export's questions ORDER BY id ASC).
    from prep.infrastructure.db import cursor

    with cursor() as c:
        rows = c.execute(
            """SELECT q.prompt FROM trivia_queue tq
                 JOIN questions q ON q.id = tq.question_id
                WHERE q.deck_id = ?
                ORDER BY tq.queue_position ASC""",
            (out.deck_id,),
        ).fetchall()
    queue_prompts = [r["prompt"] for r in rows]
    src_prompts_in_export_order = [q.prompt for q in src]
    assert queue_prompts == src_prompts_in_export_order


def test_csv_trivia_double_export_is_byte_identical(initialized_db: str):
    """Trivia version of the SRS double-export test: the preamble +
    body should reserialize byte-for-byte after a round-trip."""
    src_deck_id, _ = _seed_trivia_deck(initialized_db, "trivia-double-src")
    first = deck_to_csv(initialized_db, src_deck_id)

    out = csv_to_deck(
        initialized_db,
        "trivia-double-dst",
        first,
        deck_repo=DeckRepo(),
        question_repo=QuestionRepo(),
    )
    assert out.errors == []
    second = deck_to_csv(initialized_db, out.deck_id)
    assert first == second, (
        "second trivia export differs from first — preamble or CSV body drifted.\n"
        f"--- first ---\n{first}\n--- second ---\n{second}"
    )


def test_csv_trivia_import_rejects_type_mismatch(initialized_db: str):
    """A trivia-preamble CSV imported into an existing SRS deck name
    should fail with a clear error, not silently mix shapes."""
    # Create an SRS deck named 'collide'.
    DeckRepo().get_or_create(initialized_db, "collide")
    body = "# deck_type: trivia\n# notification_interval_minutes: 30\ntype,topic,prompt,answer\nshort,x,Q,A\n"
    out = csv_to_deck(
        initialized_db,
        "collide",
        body,
        deck_repo=DeckRepo(),
        question_repo=QuestionRepo(),
    )
    assert out.inserted == 0
    assert any("already exists" in e for e in out.errors), out.errors


def test_csv_srs_import_rejects_into_existing_trivia_deck(initialized_db: str):
    """The reverse: a plain SRS CSV imported into an existing trivia
    deck name should also fail with a clear error."""
    DeckRepo().create_trivia(initialized_db, "trivia-target", topic="x", interval_minutes=30)
    body = "type,topic,prompt,answer\nshort,x,Q,A\n"
    out = csv_to_deck(
        initialized_db,
        "trivia-target",
        body,
        deck_repo=DeckRepo(),
        question_repo=QuestionRepo(),
    )
    assert out.inserted == 0
    assert any("already exists" in e for e in out.errors), out.errors


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
