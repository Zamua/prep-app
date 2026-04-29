"""SRS (spaced-repetition scheduling) state machine.

Pure logic — no I/O, no SQL, no FastAPI. Imports only stdlib + pydantic.
This is the *domain* — `record_review` in the persistence layer
orchestrates IO around these functions, but the rules live here.

The ladder: when a card is first inserted, it sits at step 0 with a
next_due 10 minutes out. Each correct review bumps the step forward
(capped at the top), each wrong review resets to step 0. The interval
at each step grows roughly geometrically:

    step 0 →  10 minutes   (failure recovery / first sight)
    step 1 →   1 day
    step 2 →   3 days
    step 3 →   7 days
    step 4 →  14 days
    step 5 →  30 days       (terminal — stays here)

This is a simplified SM-2 — no per-card ease factor, no fractional
intervals. Easier to reason about + good enough for a personal tool.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum

# Minutes-from-now-until-next-review for each step on the ladder.
# Step is an index into this tuple. The terminal step is `len - 1`.
LADDER_MINUTES: tuple[int, ...] = (
    10,  # step 0: 10 minutes
    24 * 60,  # step 1: 1 day
    3 * 24 * 60,  # step 2: 3 days
    7 * 24 * 60,  # step 3: 7 days
    14 * 24 * 60,  # step 4: 14 days
    30 * 24 * 60,  # step 5: 30 days (terminal)
)

TERMINAL_STEP: int = len(LADDER_MINUTES) - 1


class Verdict(str, Enum):
    """Outcome of grading a single review.

    A `str` enum so it serializes naturally into JSON / SQL TEXT columns
    without explicit conversion.
    """

    RIGHT = "right"
    WRONG = "wrong"

    @property
    def is_correct(self) -> bool:
        return self is Verdict.RIGHT


def advance_step(current_step: int, verdict: Verdict) -> int:
    """Compute the next SRS step given the current step and a verdict.

    - WRONG always resets to 0 (back to "review in 10 minutes").
    - RIGHT advances by 1, capped at TERMINAL_STEP.

    Raises ValueError if `current_step` is out of bounds — the caller
    is supposed to guarantee that, but we'd rather fail loud than
    silently clamp and hide a bug.
    """
    if not 0 <= current_step <= TERMINAL_STEP:
        raise ValueError(f"current_step {current_step} out of range [0, {TERMINAL_STEP}]")
    if not verdict.is_correct:
        return 0
    return min(current_step + 1, TERMINAL_STEP)


def interval_for_step(step: int) -> timedelta:
    """The wait between review and next-due for a given step.

    Raises ValueError if `step` is out of bounds (mirrors advance_step
    for symmetry — both are caller-contract violations).
    """
    if not 0 <= step <= TERMINAL_STEP:
        raise ValueError(f"step {step} out of range [0, {TERMINAL_STEP}]")
    return timedelta(minutes=LADDER_MINUTES[step])


def next_due_at(now: datetime, step: int) -> datetime:
    """Next-due timestamp for a card that just landed on `step` at `now`.

    `now` should be timezone-aware. We don't enforce it; if you pass a
    naive datetime, the result is also naive — the caller is responsible
    for the timezone discipline they want.
    """
    return now + interval_for_step(step)
