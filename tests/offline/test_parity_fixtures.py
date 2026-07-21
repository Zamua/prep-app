"""Python side of the offline grader/ladder parity pin.

The offline app ports two pieces of pure domain logic to JS:
static/js/offline/grader.js (the deterministic grader) and
static/js/offline/scheduler.js (the local ladder). Both sides run the
SAME fixture files in tests/offline/fixtures/, so the port and the
Python truth cannot drift apart silently: this module pins Python to
the fixtures; the browser suite pins the JS modules to them.

Fixture case shape (generic on purpose, so the browser side can
dispatch without per-case knowledge):

    {"id": ..., "module": "grader"|"scheduler", "fn": ...,
     "args": [...], "expected": ...}

`expected` is the JS-side expectation. A few regex cases diverge BY
DESIGN (patterns Python grades but the JS grader refuses, falling to
self-verdict); those carry `expected_py` with the Python-side
expectation. The divergence is safe because a locally recorded
verdict replays server-side as a verdict, never re-graded (see the
regex row in the failure-modes table of docs/OFFLINE.md).

The ladder has no live Python implementation anymore (FSRS replaced
it), so the reference here is rebuilt from what prep/domain/srs.py
still exports: seed_state_from_ladder_step's stability table and
TERMINAL_STEP. test_ladder_minutes_pinned_to_srs keeps that
derivation honest.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from prep.domain import srs
from prep.domain.grading import grade, match_regex

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# The pre-FSRS ladder (docs/OFFLINE.md section 5). Index = step.
LADDER_MINUTES = [10, 1440, 4320, 10080, 20160, 43200]


def _load(name: str) -> dict[str, Any]:
    with open(FIXTURES_DIR / name, encoding="utf-8") as f:
        return json.load(f)


GRADER_FIXTURE = _load("grader_cases.json")
LADDER_FIXTURE = _load("ladder_cases.json")


def _cases(fixture: dict[str, Any], fn: str) -> list[Any]:
    return [pytest.param(c, id=c["id"]) for c in fixture["cases"] if c["fn"] == fn]


def _expected(case: dict[str, Any]) -> Any:
    """The Python-side expectation: expected_py when the engines
    deliberately diverge, else the shared expected."""
    return case["expected_py"] if "expected_py" in case else case["expected"]


# ---- grader ------------------------------------------------------------


def _grade_reference(
    card: dict[str, Any], user_answer: str, idk: bool = False
) -> dict[str, str] | None:
    """What grader.js grade() must return, re-derived from the Python
    domain: {"verdict": ...} for deterministic grades, None for the
    reveal + self-verdict flow."""
    if idk:
        return {"verdict": grade(card, user_answer, idk=True)["result"]}
    qtype = card.get("type")
    if qtype in ("mcq", "multi"):
        return {"verdict": grade(card, user_answer)["result"]}
    if qtype == "short":
        matched = match_regex(card.get("answer_regex"), user_answer)
        if matched is None:
            return None
        return {"verdict": "right" if matched else "wrong"}
    return None


@pytest.mark.parametrize("case", _cases(GRADER_FIXTURE, "grade"))
def test_grade_cases(case: dict[str, Any]) -> None:
    assert _grade_reference(*case["args"]) == _expected(case)


@pytest.mark.parametrize("case", _cases(GRADER_FIXTURE, "matchRegex"))
def test_match_regex_cases(case: dict[str, Any]) -> None:
    pattern, given = case["args"]
    assert match_regex(pattern, given) == _expected(case)


@pytest.mark.parametrize("qtype", ["short", "code", "weird"])
def test_free_text_types_refuse_sync_grading(qtype: str) -> None:
    """The null branches of the JS grader correspond to Python
    REFUSING synchronous grading for these types: pin that refusal so
    a future sync path for them forces a fixture revisit."""
    with pytest.raises(ValueError):
        grade({"type": qtype, "answer": "x"}, "x")


# ---- ladder ------------------------------------------------------------


def _instant(value: Any) -> datetime | None:
    """Parse a fixture instant the way Date.parse does for the shapes
    fixtures are allowed to contain. Naive instants are banned: JS
    Date.parse reads them as LOCAL time and Python as naive, so
    parity would be machine-dependent (this is also the M3 write
    rule: every stored timestamp is uniform-offset UTC)."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    assert parsed.tzinfo is not None, f"fixture instant {value!r} lacks an explicit offset"
    return parsed


def _transition_reference(step: Any, verdict: str) -> dict[str, int]:
    current = 0.0 if step is None else float(step)
    if not math.isfinite(current):
        current = 0.0
    clamped = max(0, min(srs.TERMINAL_STEP, int(current)))
    next_step = min(clamped + 1, srs.TERMINAL_STEP) if verdict == "right" else 0
    return {"step": next_step, "next_due_minutes": LADDER_MINUTES[next_step]}


def _due_reference(now: Any, next_due: Any) -> bool:
    if next_due is None or next_due == "":
        return True
    due_at = _instant(next_due)
    if due_at is None:
        return True
    now_at = _instant(now)
    if now_at is None:
        return False
    return due_at <= now_at


def _next_due_iso_reference(now: Any, minutes: int) -> str:
    base = _instant(now)
    assert base is not None
    result = (base + timedelta(minutes=minutes)).astimezone(timezone.utc)
    return result.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def test_ladder_minutes_pinned_to_srs() -> None:
    """The ladder table IS the pre-FSRS ladder srs.py still carries:
    steps 1-5 are seed_state_from_ladder_step's stability days (in
    minutes), step 0 is the 10-minute relearn rung of the original
    10m/1d/3d/7d/14d/30d ladder, and the top rung is TERMINAL_STEP.
    step_for_stability round-trips every seeded rung, so the local
    ladder and the snapshot's step bucket agree on what a step
    means."""
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    assert len(LADDER_MINUTES) == srs.TERMINAL_STEP + 1
    assert LADDER_MINUTES[0] == 10
    for step in range(1, srs.TERMINAL_STEP + 1):
        state = srs.seed_state_from_ladder_step(step, now=now)
        assert state.stability is not None
        assert LADDER_MINUTES[step] == int(state.stability * 24 * 60)
        assert srs.step_for_stability(state.stability) == step
    assert LADDER_FIXTURE["ladder_minutes"] == LADDER_MINUTES


@pytest.mark.parametrize("case", _cases(LADDER_FIXTURE, "transition"))
def test_transition_cases(case: dict[str, Any]) -> None:
    assert _transition_reference(*case["args"]) == _expected(case)


@pytest.mark.parametrize("case", _cases(LADDER_FIXTURE, "due"))
def test_due_cases(case: dict[str, Any]) -> None:
    assert _due_reference(*case["args"]) == _expected(case)


@pytest.mark.parametrize("case", _cases(LADDER_FIXTURE, "nextDueIso"))
def test_next_due_iso_cases(case: dict[str, Any]) -> None:
    assert _next_due_iso_reference(*case["args"]) == _expected(case)


# ---- fixture integrity -------------------------------------------------
#
# The parametrized tests above source their cases FROM the fixture
# files, so an empty or mistyped fixture would pass vacuously. These
# keep the pin falsifiable.


def test_every_case_targets_a_known_function() -> None:
    assert {c["fn"] for c in GRADER_FIXTURE["cases"]} == {"grade", "matchRegex"}
    assert {c["fn"] for c in LADDER_FIXTURE["cases"]} == {"transition", "due", "nextDueIso"}
    assert all(c["module"] == "grader" for c in GRADER_FIXTURE["cases"])
    assert all(c["module"] == "scheduler" for c in LADDER_FIXTURE["cases"])


def test_fixture_case_counts_and_unique_ids() -> None:
    assert len(GRADER_FIXTURE["cases"]) >= 40
    assert len(LADDER_FIXTURE["cases"]) >= 25
    ids = [c["id"] for c in GRADER_FIXTURE["cases"] + LADDER_FIXTURE["cases"]]
    assert len(ids) == len(set(ids))
