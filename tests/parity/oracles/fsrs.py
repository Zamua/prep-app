"""FSRS oracle: `schedule_review` over seeded start states and review
sequences, fuzz off, so another implementation can replay every
transition and match `stability` / `difficulty` to 1e-9 and every
other field exactly.

Fuzz is disabled by pre-populating the scheduler cache with
`enable_fuzzing=False` instances for each retention the corpus uses;
`prep/` is untouched. `PARITY_PERTURB_FSRS=1` bumps `w[0]` by 1e-6 to
prove the corpus test goes red on a scheduler change.
"""

from __future__ import annotations

import importlib.metadata
import os
import random
from datetime import datetime, timedelta

from fsrs import Scheduler

from prep.domain import srs
from prep.domain.srs import (
    MAX_DESIRED_RETENTION,
    MIN_DESIRED_RETENTION,
    CardSRSState,
    Verdict,
    schedule_review,
    seed_state_from_ladder_step,
    step_for_stability,
)
from tests.parity.oracles import PARITY_NOW, dump_json, pin_clock, write_corpus

NAME = "fsrs"
SEED = 20260314
SEQUENCES = 3000
MIN_TRANSITIONS = 5000

RETENTIONS = (0.5, 0.70, 0.80, 0.90, 0.95, 0.97, 0.99)
ELAPSED = {
    "0": timedelta(0),
    "1m": timedelta(minutes=1),
    "10m": timedelta(minutes=10),
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
    "3d": timedelta(days=3),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "365d": timedelta(days=365),
}
STABILITY_THRESHOLDS = (1.0, 3.0, 7.0, 14.0, 30.0)
EPSILON = 1e-6

ENV_PERTURB = "PARITY_PERTURB_FSRS"


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.isoformat()


def _state_dict(state: CardSRSState) -> dict:
    return {
        "stability": state.stability,
        "difficulty": state.difficulty,
        "fsrs_state": state.fsrs_state,
        "last_review": _iso(state.last_review),
    }


def start_states(start: datetime) -> list[tuple[str, CardSRSState]]:
    """Fresh, the five ladder seeds, and both sides of every
    `step_for_stability` threshold."""
    out: list[tuple[str, CardSRSState]] = [("fresh", CardSRSState.fresh())]
    for step in range(1, 6):
        out.append((f"ladder-{step}", seed_state_from_ladder_step(step, now=start)))
    for threshold in STABILITY_THRESHOLDS:
        for sign, label in ((-EPSILON, "below"), (EPSILON, "above")):
            out.append(
                (
                    f"stability-{threshold:g}-{label}",
                    CardSRSState(
                        stability=threshold + sign,
                        difficulty=5.0,
                        fsrs_state=2,
                        last_review=start,
                    ),
                )
            )
    return out


def _parameters() -> tuple[float, ...]:
    params = tuple(Scheduler().parameters)
    if os.environ.get(ENV_PERTURB) == "1":
        params = (params[0] + 1e-6,) + params[1:]
    return params


def _install_fuzz_free_schedulers(params: tuple[float, ...]) -> dict:
    """Pre-populate every cache key the corpus reaches (retentions are
    clamped before rounding). Returns the prior cache contents."""
    previous = dict(srs._SCHEDULER_CACHE)
    srs._SCHEDULER_CACHE.clear()
    for retention in RETENTIONS:
        clamped = max(MIN_DESIRED_RETENTION, min(MAX_DESIRED_RETENTION, retention))
        key = round(clamped, 3)
        srs._SCHEDULER_CACHE[key] = Scheduler(
            parameters=params, desired_retention=key, enable_fuzzing=False
        )
    return previous


def _restore_schedulers(previous: dict) -> None:
    srs._SCHEDULER_CACHE.clear()
    srs._SCHEDULER_CACHE.update(previous)


def build_cases(start: datetime) -> list[dict]:
    rng = random.Random(SEED)
    starts = start_states(start)
    elapsed_labels = list(ELAPSED)
    cases: list[dict] = []
    for i in range(SEQUENCES):
        start_label, state = rng.choice(starts)
        retention = rng.choice(RETENTIONS)
        length = rng.randint(1, 8)
        now = start
        reviews = []
        for _ in range(length):
            elapsed = rng.choice(elapsed_labels)
            now = now + ELAPSED[elapsed]
            verdict = rng.choice((Verdict.RIGHT, Verdict.WRONG))
            try:
                result = schedule_review(state, verdict, now=now, desired_retention=retention)
            except AssertionError:
                # py-fsrs refuses a Relearning card whose step was not
                # persisted; the corpus records the refusal and the
                # sequence ends there.
                reviews.append(
                    {
                        "input": _state_dict(state),
                        "verdict": verdict.value,
                        "elapsed": elapsed,
                        "now": now.isoformat(),
                        "error": {"type": "AssertionError"},
                    }
                )
                break
            reviews.append(
                {
                    "input": _state_dict(state),
                    "verdict": verdict.value,
                    "elapsed": elapsed,
                    "now": now.isoformat(),
                    "output": {
                        **_state_dict(result.state),
                        "next_due": result.next_due.isoformat(),
                        "interval_seconds": result.interval_seconds,
                        "step_bucket": result.step_bucket,
                    },
                }
            )
            state = result.state
        cases.append(
            {
                "id": f"seq-{i:04d}",
                "start": start_label,
                "retention": retention,
                "reviews": reviews,
            }
        )
    return cases


def extract() -> dict[str, str]:
    with pin_clock():
        params = _parameters()
        previous = _install_fuzz_free_schedulers(params)
        try:
            cases = build_cases(PARITY_NOW)
        finally:
            _restore_schedulers(previous)
    transitions = sum(1 for c in cases for r in c["reviews"] if "output" in r)
    raised = sum(1 for c in cases for r in c["reviews"] if "error" in r)
    assert transitions >= MIN_TRANSITIONS, transitions
    header = {
        "transitions_raised": raised,
        "py_fsrs_version": importlib.metadata.version("fsrs"),
        "parameters": list(params),
        "enable_fuzzing": False,
        "retention_clamp": [MIN_DESIRED_RETENTION, MAX_DESIRED_RETENTION],
        "step_thresholds": list(STABILITY_THRESHOLDS),
        "float_tolerance": 1e-9,
        "transitions": transitions,
        "seed": SEED,
        "step_bucket_of": {str(t): step_for_stability(t) for t in STABILITY_THRESHOLDS},
    }
    return {"corpus.json": dump_json({"header": header, "cases": cases})}


def main() -> None:
    root = write_corpus(NAME, extract())
    print(f"wrote {root}")


if __name__ == "__main__":
    main()
