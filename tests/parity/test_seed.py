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
