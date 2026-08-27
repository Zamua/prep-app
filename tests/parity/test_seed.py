"""A re-seed on one server hands out the ids the first seed did
(docs/PARITY-GATE.md C6): card numbers in the goldens must not
depend on how many seeds the server has already served."""

from __future__ import annotations

from prep.dev.parity_seed import seed
from prep.infrastructure.db import cursor
from tests.parity.harness.constants import PARITY_USER
from tests.parity.oracles import pin_clock


def _question_ids() -> list[int]:
    with cursor() as c:
        return [r["id"] for r in c.execute("SELECT id FROM questions ORDER BY id").fetchall()]


def test_reseed_hands_out_the_same_ids(initialized_db):
    with pin_clock():
        first = seed(PARITY_USER, "reader")
        first_ids = _question_ids()
        second = seed(PARITY_USER, "reader")
    assert first_ids and _question_ids() == first_ids
    # Session ids are minted at random by design; every row id must hold.
    first.pop("sessions")
    second.pop("sessions")
    assert second == first


def test_io_pins_what_the_import_export_and_split_flows_address(initialized_db):
    """The split flow ticks the first two boxes and the export flow opens
    both decks by slug, so the insertion order and the two slugs are part of
    the profile's contract. `worker/tests/seed.test.ts` pins the same numbers
    on the other side."""
    with pin_clock():
        ids = seed(PARITY_USER, "io")
    assert ids["decks"] == {
        "srs": {"id": 1, "slug": "algorithms", "display": "Algorithms"},
        "trivia": {"id": 2, "slug": "database-trivia", "display": "Database Trivia"},
    }
    assert ids["questions"]["srs"] == {
        "complexity": 1,
        "stability": 2,
        "binary": 3,
        "invariant": 4,
    }
    assert list(ids["questions"]["trivia"]) == ["isolation", "index", "wal"]
    with cursor() as c:
        card = c.execute("SELECT * FROM cards WHERE question_id = 3").fetchone()
        reviews = c.execute("SELECT question_id, result FROM reviews ORDER BY id").fetchall()
    assert card["step"] == 3
    assert card["next_due"] == "2026-03-16T15:00:00+00:00"
    assert [tuple(r) for r in reviews] == [(1, "right"), (2, "wrong")]


def test_workflows_pins_the_ids_the_job_flows_address(initialized_db):
    """The phase-4 flows author transform plans against these ids, so the
    profile's insertion order is part of its contract."""
    with pin_clock():
        ids = seed(PARITY_USER, "workflows")
    assert ids["decks"] == {
        "srs_a": {"id": 1, "slug": "algorithms", "display": "Algorithms"},
        "srs_b": {"id": 2, "slug": "databases", "display": "Databases"},
        "trivia": {"id": 3, "slug": "systems-trivia", "display": "Systems Trivia"},
    }
    assert ids["questions"]["srs_a"] == {
        "complexity": 1,
        "traversal": 2,
        "binary_search": 3,
        "annotated": 4,
        "retired": 5,
        "duplicate": 6,
    }
    assert ids["questions"]["srs_b"] == {"acid": 7, "btree": 8, "wal": 9}
    with cursor() as c:
        due = dict(
            c.execute(
                "SELECT question_id, next_due FROM cards WHERE question_id IN (1, 3)"
            ).fetchall()
        )
        session = c.execute("SELECT * FROM study_sessions").fetchone()
    assert due[1] == "2026-03-14T10:00:00+00:00"
    assert due[3] == "2026-03-14T12:00:00+00:00"
    assert session["deck_id"] == 1
    assert session["last_active"] == "2026-03-14T14:58:00+00:00"
