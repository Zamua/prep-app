"""The pre-cutover FSRS scheduler, frozen.

The reference `worker/domain/fsrs` was written against. Tier 3 of the
verifier schedules every migrated card twice, once here from the
snapshot row and once in the port from the cell row, and compares the
two answers: bytes agreeing is necessary, not sufficient.

Pure logic, no I/O. `schedule_review(state, verdict, now)` is the entry
point; `step_bucket` is derived from stability, never read back from a
column.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from fsrs import Card as _FsrsCard
from fsrs import Rating as _FsrsRating
from fsrs import Scheduler as _FsrsScheduler
from fsrs import State as _FsrsState

# Target the FSRS algorithm aims for: the probability the user will
# recall the card the moment it becomes due. Anki's default + the
# FSRS paper reference. Higher = more frequent reviews (more work,
# better retention); lower = the opposite.
#
# The FALLBACK when a per-user value is not supplied (or a caller passes
# None). A snapshot's per-user value lives on users.desired_retention and
# a per-deck override on decks.desired_retention.
DEFAULT_DESIRED_RETENTION = 0.90
MIN_DESIRED_RETENTION = 0.70
MAX_DESIRED_RETENTION = 0.97
_DESIRED_RETENTION = DEFAULT_DESIRED_RETENTION  # legacy name; kept for any external import


class Verdict(str, Enum):
    """Outcome of grading a single review.

    Maps to FSRS's 4-rating scale: prep currently only emits two
    verdicts so the other half of the FSRS scale (Hard / Easy) is
    unused. Future trivia-grader work could surface a wider rating
    set; the wire shape stays a `str` so SQL TEXT columns keep their
    existing reads."""

    RIGHT = "right"
    WRONG = "wrong"

    @property
    def is_correct(self) -> bool:
        return self is Verdict.RIGHT

    def _to_fsrs(self) -> _FsrsRating:
        return _FsrsRating.Good if self.is_correct else _FsrsRating.Again


@dataclass(frozen=True)
class CardSRSState:
    """Every field the scheduler needs to compute the next review.

    `stability` / `difficulty` are None on a fresh card: FSRS
    initializes them on the first review. `fsrs_state` is the
    library's phase enum (Learning=1, Review=2, Relearning=3); we
    store it as an int.

    `last_review` is the timestamp of the most recent review, or
    None for a never-studied card. FSRS uses the elapsed time
    between this and `now` to discount stability before scoring."""

    stability: float | None
    difficulty: float | None
    fsrs_state: int
    last_review: datetime | None

    @classmethod
    def fresh(cls) -> CardSRSState:
        """A never-studied card. The cards table writes this shape
        on insert."""
        return cls(
            stability=None,
            difficulty=None,
            fsrs_state=int(_FsrsState.Learning),
            last_review=None,
        )


@dataclass(frozen=True)
class ScheduledReview:
    """Result of feeding a review through the scheduler.

    `next_due` is timezone-aware UTC. `interval_seconds` is the
    elapsed-time prediction the scheduler picked (next_due - now);
    repos persist it as minutes elsewhere for compatibility.
    `step_bucket` is the legacy 0–5 maturity bucket templates read.
    """

    state: CardSRSState
    next_due: datetime
    interval_seconds: int
    step_bucket: int


# Scheduler cache keyed on retention. The upstream Scheduler is
# parameter-bag-only (immutable weights + retention) so it's safe to
# reuse across threads: but each distinct retention value needs its
# own instance. Small footprint (most installs only have a handful
# of distinct values across all users), zero allocation on the hot
# path once warm.
_SCHEDULER_CACHE: dict[float, _FsrsScheduler] = {}


def _scheduler_for(retention: float) -> _FsrsScheduler:
    key = round(retention, 3)
    if key not in _SCHEDULER_CACHE:
        _SCHEDULER_CACHE[key] = _FsrsScheduler(desired_retention=key)
    return _SCHEDULER_CACHE[key]


def schedule_review(
    state: CardSRSState,
    verdict: Verdict,
    *,
    now: datetime,
    desired_retention: float | None = None,
) -> ScheduledReview:
    """Pure scheduler call. State + verdict + now → new state.

    `now` is the caller's instant: the domain reads no clock. The
    library wants timezone-aware datetimes; naive inputs are coerced
    to UTC rather than rejected.

    `desired_retention` defaults to the FSRS paper / Anki convention
    of 0.90; the verifier resolves a card's own value from the snapshot
    and passes it. Clamped to [MIN, MAX] to keep the scheduler
    well-behaved.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    retention = desired_retention if desired_retention is not None else DEFAULT_DESIRED_RETENTION
    retention = max(MIN_DESIRED_RETENTION, min(MAX_DESIRED_RETENTION, retention))

    card = _FsrsCard(
        state=_FsrsState(state.fsrs_state) if state.fsrs_state else _FsrsState.Learning,
        stability=state.stability,
        difficulty=state.difficulty,
        last_review=state.last_review,
        due=now,
    )
    updated, _log = _scheduler_for(retention).review_card(
        card,
        verdict._to_fsrs(),
        review_datetime=now,
    )
    next_due = updated.due
    if next_due.tzinfo is None:
        next_due = next_due.replace(tzinfo=timezone.utc)
    interval_seconds = max(0, int((next_due - now).total_seconds()))
    new_state = CardSRSState(
        stability=updated.stability,
        difficulty=updated.difficulty,
        fsrs_state=int(updated.state),
        last_review=now,
    )
    return ScheduledReview(
        state=new_state,
        next_due=next_due,
        interval_seconds=interval_seconds,
        step_bucket=step_for_stability(updated.stability),
    )


# ---- legacy compatibility ----------------------------------------------
#
# The old ladder exposed `step` (0–5) so templates could show "step 3"
# or filter by maturity. With FSRS, the closest equivalent is a
# stability-based bucket. The thresholds mirror the old ladder's
# intervals so a card that used to read "step 3 (7d)" reads roughly
# the same now.


def step_for_stability(stability: float | None) -> int:
    """Map an FSRS stability (days) to the old 0–5 step bucket.

    Used by anything that wants a coarse "how mature is this card"
    integer. Templates + DeckCard entity readers don't need to know
    FSRS exists.
    """
    if stability is None:
        return 0
    if stability < 1:
        return 0
    if stability < 3:
        return 1
    if stability < 7:
        return 2
    if stability < 14:
        return 3
    if stability < 30:
        return 4
    return 5


def seed_state_from_ladder_step(step: int, *, now: datetime) -> CardSRSState:
    """Migration helper: produce a starting FSRS state for a card
    that's currently sitting at ladder `step`.

    Used by the cards-table migration once at boot. Not part of the
    runtime scheduler path. Pick stability values that match the
    intervals the card had reached:
        step 0 → fresh (never reviewed)
        step 1 → stability=1 day
        step 2 → stability=3
        step 3 → stability=7
        step 4 → stability=14
        step 5 → stability=30
    Difficulty defaults to 5 (the FSRS-paper midpoint) since we
    don't have per-card review history to optimize on.
    """
    if step <= 0:
        return CardSRSState.fresh()
    stability_by_step = {1: 1.0, 2: 3.0, 3: 7.0, 4: 14.0, 5: 30.0}
    return CardSRSState(
        stability=stability_by_step.get(step, 30.0),
        difficulty=5.0,
        fsrs_state=int(_FsrsState.Review),
        last_review=now,
    )


# TERMINAL_STEP: kept for the few templates / aggregations that
# still ask "is this card at the top of the ladder." Equivalent to
# the highest bucket step_for_stability emits.
TERMINAL_STEP: int = 5
