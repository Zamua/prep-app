"""Domain tests for the SRS state machine.

These are pure unit tests — no DB, no FastAPI, no I/O. The whole
point of `prep/domain/` is that it's testable in isolation, and these
tests are the proof.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from prep.domain.srs import (
    LADDER_MINUTES,
    TERMINAL_STEP,
    Verdict,
    advance_step,
    interval_for_step,
    next_due_at,
)

# ---- ladder shape -------------------------------------------------------


def test_ladder_has_six_steps():
    assert len(LADDER_MINUTES) == 6


def test_ladder_is_monotonically_increasing():
    """If we ever reorder the ladder, this is a sanity check."""
    assert list(LADDER_MINUTES) == sorted(LADDER_MINUTES)


def test_ladder_matches_documented_intervals():
    """Pin the exact intervals — the user-visible promise of the app
    ('10m → 1d → 3d → 7d → 14d → 30d') depends on these exact values.
    Changing any of them is a behavior change worth a docs update."""
    assert LADDER_MINUTES == (
        10,
        1 * 24 * 60,
        3 * 24 * 60,
        7 * 24 * 60,
        14 * 24 * 60,
        30 * 24 * 60,
    )


def test_terminal_step_is_last_index():
    assert TERMINAL_STEP == len(LADDER_MINUTES) - 1
    assert TERMINAL_STEP == 5


# ---- Verdict ------------------------------------------------------------


def test_verdict_serializes_as_string():
    """The enum is `str`-backed so JSON/SQL paths work without conversion."""
    assert Verdict.RIGHT == "right"
    assert Verdict.WRONG == "wrong"
    assert Verdict("right") is Verdict.RIGHT
    assert Verdict("wrong") is Verdict.WRONG


def test_verdict_is_correct_property():
    assert Verdict.RIGHT.is_correct is True
    assert Verdict.WRONG.is_correct is False


# ---- advance_step -------------------------------------------------------


@pytest.mark.parametrize("current", range(TERMINAL_STEP + 1))
def test_wrong_resets_to_zero_from_any_step(current: int):
    assert advance_step(current, Verdict.WRONG) == 0


def test_right_advances_one_step():
    assert advance_step(0, Verdict.RIGHT) == 1
    assert advance_step(1, Verdict.RIGHT) == 2
    assert advance_step(2, Verdict.RIGHT) == 3
    assert advance_step(3, Verdict.RIGHT) == 4
    assert advance_step(4, Verdict.RIGHT) == 5


def test_right_caps_at_terminal():
    """Already-mastered cards stay at the top — they don't fall off."""
    assert advance_step(TERMINAL_STEP, Verdict.RIGHT) == TERMINAL_STEP


def test_advance_step_rejects_negative():
    with pytest.raises(ValueError, match="out of range"):
        advance_step(-1, Verdict.RIGHT)


def test_advance_step_rejects_above_terminal():
    with pytest.raises(ValueError, match="out of range"):
        advance_step(TERMINAL_STEP + 1, Verdict.WRONG)


# ---- interval_for_step --------------------------------------------------


def test_interval_step_0_is_ten_minutes():
    assert interval_for_step(0) == timedelta(minutes=10)


def test_interval_step_5_is_thirty_days():
    assert interval_for_step(5) == timedelta(days=30)


def test_interval_for_step_rejects_out_of_range():
    with pytest.raises(ValueError):
        interval_for_step(-1)
    with pytest.raises(ValueError):
        interval_for_step(TERMINAL_STEP + 1)


# ---- next_due_at --------------------------------------------------------


def test_next_due_at_step_0_is_now_plus_ten_minutes():
    now = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)
    assert next_due_at(now, 0) == datetime(2026, 4, 29, 12, 10, 0, tzinfo=timezone.utc)


def test_next_due_at_step_5_is_now_plus_thirty_days():
    now = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)
    assert next_due_at(now, 5) == datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)


def test_next_due_preserves_tz():
    """Timezone-aware in → timezone-aware out."""
    now = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)
    result = next_due_at(now, 1)
    assert result.tzinfo == timezone.utc


# ---- end-to-end ladder walk --------------------------------------------


def test_full_correct_run_walks_the_ladder():
    """A user who answers right every time hits the terminal in 5 reviews."""
    step = 0
    for expected_next in [1, 2, 3, 4, 5, 5]:  # last one stays at terminal
        step = advance_step(step, Verdict.RIGHT)
        assert step == expected_next


def test_one_wrong_at_top_falls_to_zero():
    """The whole point of the algorithm: forgetting recovery."""
    step = TERMINAL_STEP
    step = advance_step(step, Verdict.WRONG)
    assert step == 0
