"""Domain tests for the FSRS scheduler wrapper.

Pure unit tests — no DB, no FastAPI, no I/O. The whole point of
`prep/domain/` is that it's testable in isolation.

The wrapper sits on top of the upstream `fsrs` library; these tests
don't try to re-validate FSRS's math (the library has its own
exhaustive suite). Instead, they pin the prep-side contract:

- Verdict maps cleanly to FSRS Rating
- A right answer pushes next_due into the future
- A wrong answer keeps the card in / returns it to short-interval
  rehearsal
- step_for_stability maps stability days to the legacy 0–5 bucket
- seed_state_from_ladder_step lets the migration land in-flight
  cards on the new scheduler without losing maturity
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from prep.domain.srs import (
    TERMINAL_STEP,
    CardSRSState,
    Verdict,
    schedule_review,
    seed_state_from_ladder_step,
    step_for_stability,
)

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


# ---- step_for_stability -------------------------------------------------


def test_step_for_stability_thresholds():
    """The legacy ladder ran 0/1d/3d/7d/14d/30d; the bucket function
    mirrors those bands so templates that say 'step 3' still look
    roughly right."""
    assert step_for_stability(None) == 0
    assert step_for_stability(0.5) == 0
    assert step_for_stability(1.0) == 1
    assert step_for_stability(2.9) == 1
    assert step_for_stability(3.0) == 2
    assert step_for_stability(6.9) == 2
    assert step_for_stability(7.0) == 3
    assert step_for_stability(13.9) == 3
    assert step_for_stability(14.0) == 4
    assert step_for_stability(29.9) == 4
    assert step_for_stability(30.0) == 5
    assert step_for_stability(1000.0) == 5


def test_terminal_step_constant_intact():
    """Some readers still check 'is this card maxed out'."""
    assert TERMINAL_STEP == 5


# ---- schedule_review ----------------------------------------------------


def test_first_correct_review_creates_stability():
    """Fresh card + Good rating → stability gets initialized, next_due
    is in the future."""
    now = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)
    state = CardSRSState.fresh()
    result = schedule_review(state, Verdict.RIGHT, now=now)
    assert result.state.stability is not None
    assert result.state.stability > 0
    assert result.state.difficulty is not None
    assert result.next_due > now
    assert result.interval_seconds > 0
    assert result.state.last_review == now


def test_wrong_review_lands_in_short_relearning():
    """A failure on a mature card sends it back to short-interval
    rehearsal — the legacy ladder reset to 10 minutes; FSRS picks a
    similar (sub-day) reschedule."""
    now = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)
    state = CardSRSState(
        stability=30.0,
        difficulty=5.0,
        fsrs_state=2,  # Review
        last_review=now - timedelta(days=30),
    )
    result = schedule_review(state, Verdict.WRONG, now=now)
    # Interval should be much shorter than the prior 30d.
    assert result.next_due - now < timedelta(days=2)
    # And stability should drop.
    assert result.state.stability < 30.0


def test_correct_review_extends_interval():
    """Repeated correct reviews stretch the interval out — this is
    the FSRS contract we lean on."""
    now = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)
    state = CardSRSState(
        stability=7.0,
        difficulty=5.0,
        fsrs_state=2,
        last_review=now - timedelta(days=7),
    )
    result = schedule_review(state, Verdict.RIGHT, now=now)
    assert result.state.stability > 7.0
    # next_due should be at least ~a week out for a stability=7 card.
    assert result.next_due - now > timedelta(days=4)


def test_naive_datetime_is_coerced_to_utc():
    """The library is strict about tz-aware datetimes; we coerce
    naive inputs at the boundary so callers don't have to think."""
    naive = datetime(2026, 4, 29, 12, 0, 0)
    state = CardSRSState.fresh()
    result = schedule_review(state, Verdict.RIGHT, now=naive)
    assert result.next_due.tzinfo is not None


# ---- seed_state_from_ladder_step ----------------------------------------


def test_seed_step_0_is_fresh():
    """Brand-new cards (step 0) get the same state a fresh insert
    would — FSRS initializes on the first review."""
    state = seed_state_from_ladder_step(0)
    assert state.stability is None
    assert state.difficulty is None


def test_seed_higher_steps_pick_matching_stability():
    """The migration's job: someone at ladder step 3 (7-day interval)
    lands with stability=7 in FSRS terms — they don't lose maturity
    on swap day."""
    now = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)
    assert seed_state_from_ladder_step(1, now=now).stability == 1.0
    assert seed_state_from_ladder_step(2, now=now).stability == 3.0
    assert seed_state_from_ladder_step(3, now=now).stability == 7.0
    assert seed_state_from_ladder_step(4, now=now).stability == 14.0
    assert seed_state_from_ladder_step(5, now=now).stability == 30.0
    # Difficulty starts at the FSRS-paper midpoint.
    assert seed_state_from_ladder_step(3, now=now).difficulty == 5.0
