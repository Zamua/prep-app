"""End-to-end `.prepdeck` round-trip tests.

These pin the comprehensive round-trip contract: a `.prepdeck` archive
should restore a deck byte-for-byte identical to the source. Both deck
types covered (SRS + trivia). Backward-compat tests pin the
format-version refusal rules so a future writer can't silently produce
an archive an older importer would mangle.
"""

from __future__ import annotations

import io
import json
import zipfile

from prep.decks.archive import (
    FORMAT_VERSION,
    deck_to_prepdeck,
    prepdeck_to_deck,
)
from prep.decks.entities import DeckType, NewQuestion, QuestionType
from prep.decks.repo import DeckRepo, QuestionRepo
from prep.study.repo import ReviewRepo

# ---- helpers ------------------------------------------------------------


def _seed_srs_deck_with_history(initialized_db: str, name: str = "ret-src") -> int:
    """Seed an SRS deck with three cards and a non-trivial review log.
    Returns the source deck_id."""
    import json as _json

    deck_repo = DeckRepo()
    qr = QuestionRepo()
    deck_id = deck_repo.create(initialized_db, name)
    # One card of each type that exercises all CSV fields.
    qr.add(
        initialized_db,
        deck_id,
        NewQuestion(
            type=QuestionType.SHORT,
            topic="biology",
            prompt="What organelle generates ATP?",
            answer="mitochondria",
            answer_regex="mitochondri[ao]n?",
            rubric="must name the organelle",
            explanation="Mitochondria run oxidative phosphorylation.",
        ),
    )
    qr.add(
        initialized_db,
        deck_id,
        NewQuestion(
            type=QuestionType.MCQ,
            topic="geography",
            prompt="Capital of Australia?",
            answer="Canberra",
            choices=["Sydney", "Melbourne", "Canberra", "Perth"],
            rubric="one correct city",
            explanation="Compromise capital between Sydney and Melbourne.",
        ),
    )
    qr.add(
        initialized_db,
        deck_id,
        NewQuestion(
            type=QuestionType.MULTI,
            topic="systems",
            prompt="Which of these are ACID properties?",
            answer=_json.dumps(["atomicity", "isolation"]),
            choices=["atomicity", "scaling", "isolation", "latency"],
            rubric="exactly the two ACID properties",
            explanation="ACID = atomicity, consistency, isolation, durability.",
        ),
    )

    # Drive some FSRS state by recording reviews. ReviewRepo.record()
    # uses the live scheduler, which is what we want — exporter then
    # importer should round-trip whatever stable/diff/state the live
    # scheduler produced.
    rr = ReviewRepo()
    cards = qr.list_in_deck(initialized_db, deck_id)
    rr.record(initialized_db, cards[0].id, "right", user_answer="mitochondria")
    rr.record(initialized_db, cards[0].id, "right", user_answer="mitochondrion")
    rr.record(initialized_db, cards[1].id, "wrong", user_answer="Sydney")
    rr.record(initialized_db, cards[1].id, "right", user_answer="Canberra")
    rr.record(initialized_db, cards[2].id, "right", user_answer="atomicity,isolation")
    return deck_id


def _seed_trivia_deck_with_state(initialized_db: str, name: str = "trivia-src") -> int:
    """Seed a trivia deck + its queue + simulate some answered cards
    so the export carries non-trivial last_answered state."""
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
        ("Capital of France?", "Paris"),
        ("Capital of Japan?", "Tokyo"),
        ("Capital of Brazil?", "Brasília"),
    ]:
        qid = qr.add(
            initialized_db,
            deck_id,
            NewQuestion(
                type=QuestionType.SHORT,
                topic="capitals",
                prompt=prompt,
                answer=ans,
                explanation=f"{ans} is the capital.",
            ),
        )
        tq.append_card(qid, deck_id)
        qids.append(qid)
    # Mark first card answered correctly, second wrong, third untouched.
    tq.mark_answered(qids[0], True)
    tq.mark_answered(qids[1], False)
    return deck_id


# ---- meta + structure ---------------------------------------------------


def test_prepdeck_export_emits_required_entries(initialized_db: str):
    """Archive includes meta.json, cards.csv, reviews.csv for SRS;
    plus trivia_queue.csv for trivia decks."""
    src = _seed_srs_deck_with_history(initialized_db, "srs-shape")
    blob = deck_to_prepdeck(initialized_db, src)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = set(zf.namelist())
    assert names == {"meta.json", "cards.csv", "reviews.csv"}, names

    src_tr = _seed_trivia_deck_with_state(initialized_db, "trivia-shape")
    blob = deck_to_prepdeck(initialized_db, src_tr)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = set(zf.namelist())
    assert names == {"meta.json", "cards.csv", "reviews.csv", "trivia_queue.csv"}, names


def test_prepdeck_meta_carries_format_version_and_deck_config(initialized_db: str):
    """meta.json must announce the format_version + deck shape so
    older / newer importers know what they're looking at."""
    src = _seed_trivia_deck_with_state(initialized_db, "meta-shape")
    blob = deck_to_prepdeck(initialized_db, src)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        meta = json.loads(zf.read("meta.json"))
    assert meta["format_version"] == FORMAT_VERSION
    assert meta["deck"]["name"] == "meta-shape"
    assert meta["deck"]["deck_type"] == "trivia"
    assert meta["deck"]["notification_interval_minutes"] == 45
    assert meta["deck"]["trivia_session_size"] == 5
    assert meta["deck"]["context_prompt"] == "world capitals"


# ---- comprehensive round-trip ------------------------------------------


def test_prepdeck_srs_round_trip_preserves_state_and_reviews(initialized_db: str):
    """SRS deck → .prepdeck → import → identical content + FSRS state
    + reviews log."""
    src_id = _seed_srs_deck_with_history(initialized_db, "srs-roundtrip-src")
    blob = deck_to_prepdeck(initialized_db, src_id)

    out = prepdeck_to_deck(
        initialized_db,
        "srs-roundtrip-dst",
        blob,
        deck_repo=DeckRepo(),
        question_repo=QuestionRepo(),
    )
    assert out.errors == [], out.errors
    assert out.inserted == 3
    assert out.reviews_inserted == 5

    # Content equivalence.
    from prep.decks.io import _questions_for_export

    src = _questions_for_export(initialized_db, src_id)
    dst = _questions_for_export(initialized_db, out.deck_id)
    src_by_prompt = {q.prompt: q for q in src}
    dst_by_prompt = {q.prompt: q for q in dst}
    assert set(src_by_prompt) == set(dst_by_prompt)
    for prompt, s in src_by_prompt.items():
        d = dst_by_prompt[prompt]
        assert (s.type, s.topic, s.answer, s.choices, s.rubric, s.explanation) == (
            d.type,
            d.topic,
            d.answer,
            d.choices,
            d.rubric,
            d.explanation,
        )
        assert s.answer_regex == d.answer_regex
        assert s.skeleton == d.skeleton
        assert s.language == d.language

    # FSRS state equivalence on the cards rows.
    from prep.infrastructure.db import cursor

    def _state_by_prompt(deck_id: int) -> dict[str, dict]:
        with cursor() as c:
            rows = c.execute(
                """SELECT q.prompt, c.step, c.next_due, c.last_review,
                          c.stability, c.difficulty, c.fsrs_state
                     FROM cards c JOIN questions q ON q.id = c.question_id
                    WHERE q.deck_id = ?""",
                (deck_id,),
            ).fetchall()
        return {r["prompt"]: dict(r) for r in rows}

    s_state = _state_by_prompt(src_id)
    d_state = _state_by_prompt(out.deck_id)
    assert set(s_state) == set(d_state)
    for prompt in s_state:
        s = s_state[prompt]
        d = d_state[prompt]
        assert s["step"] == d["step"]
        assert s["next_due"] == d["next_due"]
        assert s["last_review"] == d["last_review"]
        assert s["fsrs_state"] == d["fsrs_state"]
        # Floats are emitted with %.6g — round-trip should be exact
        # within that precision.
        for col in ("stability", "difficulty"):
            sv, dv = s[col], d[col]
            if sv is None or dv is None:
                assert sv == dv, f"{prompt}: {col} {sv!r} vs {dv!r}"
            else:
                assert abs(sv - dv) < 1e-5, f"{prompt}: {col} {sv} vs {dv}"

    # Review log equivalence (count + per-card result counts).
    def _review_summary(deck_id: int) -> dict[str, tuple[int, int]]:
        with cursor() as c:
            rows = c.execute(
                """SELECT q.prompt,
                          SUM(CASE WHEN r.result='right' THEN 1 ELSE 0 END) AS right_n,
                          SUM(CASE WHEN r.result='wrong' THEN 1 ELSE 0 END) AS wrong_n
                     FROM reviews r JOIN questions q ON q.id = r.question_id
                    WHERE q.deck_id = ?
                    GROUP BY q.prompt""",
                (deck_id,),
            ).fetchall()
        return {r["prompt"]: (int(r["right_n"]), int(r["wrong_n"])) for r in rows}

    assert _review_summary(src_id) == _review_summary(out.deck_id)


def test_prepdeck_trivia_round_trip_preserves_queue_state(initialized_db: str):
    """Trivia deck → .prepdeck → import → preserves deck-level config,
    trivia_queue order, AND per-card last_answered state."""
    src_id = _seed_trivia_deck_with_state(initialized_db, "trivia-roundtrip-src")
    blob = deck_to_prepdeck(initialized_db, src_id)

    out = prepdeck_to_deck(
        initialized_db,
        "trivia-roundtrip-dst",
        blob,
        deck_repo=DeckRepo(),
        question_repo=QuestionRepo(),
    )
    assert out.errors == [], out.errors
    assert out.inserted == 3
    assert out.queue_rows_inserted == 3

    deck_repo = DeckRepo()
    assert deck_repo.get_type(initialized_db, out.deck_id) == DeckType.TRIVIA
    meta = deck_repo.get_meta(initialized_db, out.deck_id)
    assert meta.interval_minutes == 45
    assert meta.session_size == 5
    assert meta.context_prompt == "world capitals"

    # trivia_queue per-card state survives — including the answered /
    # not-answered distinction.
    from prep.infrastructure.db import cursor

    def _queue_summary(deck_id: int) -> list[tuple[str, str | None, int | None, int]]:
        with cursor() as c:
            rows = c.execute(
                """SELECT q.prompt, tq.last_answered_at,
                          tq.last_answered_correctly, tq.queue_position
                     FROM trivia_queue tq JOIN questions q ON q.id = tq.question_id
                    WHERE q.deck_id = ?
                    ORDER BY tq.queue_position ASC""",
                (deck_id,),
            ).fetchall()
        return [
            (
                r["prompt"],
                r["last_answered_at"],
                None if r["last_answered_correctly"] is None else int(r["last_answered_correctly"]),
                int(r["queue_position"]),
            )
            for r in rows
        ]

    # Source and destination queues should have identical per-prompt
    # state. Positions may differ in absolute value (post-answer
    # rotation reorders the queue on source) but the per-prompt
    # tuple sets should match modulo position.
    src_summary = _queue_summary(src_id)
    dst_summary = _queue_summary(out.deck_id)
    by_prompt_src = {p: (la, lac) for p, la, lac, _pos in src_summary}
    by_prompt_dst = {p: (la, lac) for p, la, lac, _pos in dst_summary}
    assert by_prompt_src == by_prompt_dst


def test_prepdeck_double_export_is_byte_identical(initialized_db: str):
    """Export → import → export should yield byte-identical archives
    (modulo `exported_at`, which is the only non-deterministic field).
    Catches silent re-serialization drift that a content-level diff
    might miss."""
    src_id = _seed_srs_deck_with_history(initialized_db, "double-srs")
    first = deck_to_prepdeck(initialized_db, src_id)
    out = prepdeck_to_deck(
        initialized_db,
        "double-srs-dst",
        first,
        deck_repo=DeckRepo(),
        question_repo=QuestionRepo(),
    )
    assert out.errors == [], out.errors
    second = deck_to_prepdeck(initialized_db, out.deck_id)

    # Compare each section other than meta.json (which carries the
    # timestamp). The deck name and exported_at vary; everything else
    # should match byte-for-byte.
    with zipfile.ZipFile(io.BytesIO(first)) as zf:
        first_sections = {n: zf.read(n) for n in zf.namelist()}
    with zipfile.ZipFile(io.BytesIO(second)) as zf:
        second_sections = {n: zf.read(n) for n in zf.namelist()}
    assert set(first_sections) == set(second_sections)
    for name in first_sections:
        if name == "meta.json":
            # Compare structure, not timestamp + deck name (which differ
            # on purpose).
            m1 = json.loads(first_sections[name])
            m2 = json.loads(second_sections[name])
            m1["deck"].pop("name", None)
            m2["deck"].pop("name", None)
            m1.pop("exported_at", None)
            m2.pop("exported_at", None)
            assert m1 == m2
        else:
            assert (
                first_sections[name] == second_sections[name]
            ), f"section {name} drifted between export #1 and export #2"


# ---- format-version backward compatibility -----------------------------


def test_prepdeck_rejects_future_format_version(initialized_db: str):
    """A v999 archive imports into a v1 build → clear error, no
    partial deck creation."""
    src_id = _seed_srs_deck_with_history(initialized_db, "future-version-src")
    blob = deck_to_prepdeck(initialized_db, src_id)

    # Rewrite meta.json to claim a future version.
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(blob)) as src_zf:
        with zipfile.ZipFile(buf, "w") as dst_zf:
            for name in src_zf.namelist():
                if name == "meta.json":
                    meta = json.loads(src_zf.read("meta.json"))
                    meta["format_version"] = 999
                    dst_zf.writestr("meta.json", json.dumps(meta))
                else:
                    dst_zf.writestr(name, src_zf.read(name))
    out = prepdeck_to_deck(
        initialized_db,
        "future-version-dst",
        buf.getvalue(),
        deck_repo=DeckRepo(),
        question_repo=QuestionRepo(),
    )
    assert out.inserted == 0
    assert out.errors and "format_version 999" in out.errors[0]


def test_prepdeck_rejects_non_zip(initialized_db: str):
    out = prepdeck_to_deck(
        initialized_db,
        "bad-zip",
        b"not a zip file",
        deck_repo=DeckRepo(),
        question_repo=QuestionRepo(),
    )
    assert out.inserted == 0
    assert out.errors and "not a valid zip" in out.errors[0]


def test_prepdeck_rejects_missing_entries(initialized_db: str):
    """An archive without cards.csv or reviews.csv should fail
    cleanly, not partially-create the deck."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("meta.json", json.dumps({"format_version": 1, "deck": {"name": "x"}}))
    out = prepdeck_to_deck(
        initialized_db,
        "missing-entries",
        buf.getvalue(),
        deck_repo=DeckRepo(),
        question_repo=QuestionRepo(),
    )
    assert out.inserted == 0
    assert out.errors and "missing required entries" in out.errors[0]
    # No partial deck was created.
    assert DeckRepo().find_id(initialized_db, "missing-entries") is None


def test_prepdeck_refuses_into_existing_deck(initialized_db: str):
    """Restore semantics — refuse to import into an existing deck name."""
    src_id = _seed_srs_deck_with_history(initialized_db, "exists-src")
    blob = deck_to_prepdeck(initialized_db, src_id)
    DeckRepo().create(initialized_db, "exists-target")
    out = prepdeck_to_deck(
        initialized_db,
        "exists-target",
        blob,
        deck_repo=DeckRepo(),
        question_repo=QuestionRepo(),
    )
    assert out.inserted == 0
    assert out.errors and "already exists" in out.errors[0]


# ---- HTTP route smoke tests --------------------------------------------


def test_export_hub_renders_three_format_buttons(client, initialized_db: str):
    """GET /deck/<name>/export → 200 with cards for all three formats."""
    _seed_srs_deck_with_history(initialized_db, "hub-deck")
    r = client.get("/deck/hub-deck/export")
    assert r.status_code == 200
    # Each format has an Export button with its data-export-url.
    assert "/deck/hub-deck/export.prepdeck" in r.text
    assert "/deck/hub-deck/export.csv" in r.text
    assert "/deck/hub-deck/export.apkg" in r.text
    # Each card's Export button is hooked by the deck-export.js module.
    assert r.text.count('class="btn btn-primary export-btn"') == 3


def test_export_hub_404s_for_missing_deck(client, initialized_db: str):
    r = client.get("/deck/no-such-deck/export")
    assert r.status_code == 404


def test_export_prepdeck_route_returns_zip(client, initialized_db: str):
    """GET /deck/<name>/export.prepdeck → 200, application/zip with the
    right content-disposition, body is a valid zip carrying the
    expected sections."""
    _seed_srs_deck_with_history(initialized_db, "route-export")
    r = client.get("/deck/route-export/export.prepdeck")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert 'attachment; filename="route-export.prepdeck"' in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = set(zf.namelist())
    assert {"meta.json", "cards.csv", "reviews.csv"} <= names


def test_export_prepdeck_route_404s_for_missing_deck(client, initialized_db: str):
    r = client.get("/deck/no-such-deck/export.prepdeck")
    assert r.status_code == 404


def test_import_prepdeck_route_renders_outcome(client, initialized_db: str):
    """POST /decks/import-prepdeck with a valid archive uploads + the
    outcome page renders with the restored counts."""
    src_id = _seed_srs_deck_with_history(initialized_db, "route-src")
    blob = deck_to_prepdeck(initialized_db, src_id)
    files = {"file": ("route-src.prepdeck", blob, "application/zip")}
    r = client.post(
        "/decks/import-prepdeck",
        data={"name": "route-restored"},
        files=files,
    )
    assert r.status_code == 200
    assert "Restored into" in r.text
    assert "route-restored" in r.text
    # 3 cards + 5 reviews come from the seed helper.
    assert "<strong>3</strong>" in r.text
    assert "<strong>5</strong>" in r.text
