"""Synchronous grader for the deterministic question types.

Pure logic — no I/O, no DB, no HTTP. Used by the FastAPI request
thread for `mcq`, `multi`, and "I don't know" submissions: these
resolve in microseconds and don't need a workflow.

Free-text grading (`code` / `short` answers requiring an LLM judge)
is NOT here; that path goes through the Temporal worker so the
request doesn't block on a multi-second claude call. See:

  - prep/app.py: study_submit / session_submit dispatch the slow
    path to temporal_client.start_grading
  - worker-go/activities/grading.go: the GradeFreeText activity

`match_regex` + `validate_regex_update` support the SHORT-trivia
two-phase grader: the deterministic regex match runs first; claude
fallback only fires when the pattern is missing or fails. The
re-grade flow may then ask claude to propose an evolved regex,
which `validate_regex_update` admits only if it compiles, accepts
the canonical literal answer, and isn't suspiciously catastrophic.
"""

from __future__ import annotations

import json
import re
from typing import Any

from prep.domain.srs import Verdict

# Hard cap on regex pattern length we'll accept from claude. Anything
# longer is almost certainly a hallucination; trivia answers are
# 1-5 words, the regex shouldn't be a novel.
MAX_REGEX_LEN = 500


def grade(question: dict[str, Any], user_answer: str, idk: bool = False) -> dict[str, Any]:
    """Grade a question synchronously.

    Returns a dict with:
      - result: 'right' | 'wrong' (the Verdict enum value, str-form)
      - feedback: short human-readable explanation
      - model_answer_summary: a snippet of the canonical answer

    The shape is dict-typed (not a pydantic model) for now because
    callers in app.py + db.py both consume it as a dict; we'll
    introduce GradeResult-as-pydantic when we extract the study
    bounded context (Phase 6).

    Raises ValueError for free-text question types — those don't
    belong on this synchronous path.
    """
    if idk:
        return {
            "result": Verdict.WRONG.value,
            "feedback": "Marked as 'I don't know' — see again soon.",
            "model_answer_summary": (question.get("answer") or "")[:400],
        }
    qtype = question["type"]
    if qtype == "mcq":
        return _grade_mcq(question, user_answer)
    if qtype == "multi":
        return _grade_multi(question, user_answer)
    raise ValueError(
        f"grade() called with type={qtype!r}; free-text grading goes "
        f"through the Temporal worker (GradeFreeText activity), not "
        f"this synchronous helper."
    )


def _grade_mcq(question: dict[str, Any], user_answer: str) -> dict[str, Any]:
    correct = (user_answer or "").strip() == (question["answer"] or "").strip()
    return {
        "result": (Verdict.RIGHT if correct else Verdict.WRONG).value,
        "feedback": "Correct." if correct else "Wrong choice.",
        "model_answer_summary": question["answer"],
    }


def match_regex(pattern: str | None, given: str) -> bool | None:
    """Try to match `given` against `pattern` (case-insensitive,
    fullmatch with whitespace tolerance at the boundaries).

    Returns:
      - True  → the pattern matched
      - False → the pattern compiled but didn't match
      - None  → no pattern provided OR pattern is unusable
                (caller falls back to the next grader)

    None vs False is an important distinction: a returned False is
    "claude generated a regex and the user's answer doesn't satisfy
    it"; a returned None means "we can't trust this regex to grade,
    use the legacy path."
    """
    if not pattern:
        return None
    if len(pattern) > MAX_REGEX_LEN:
        return None
    try:
        compiled = re.compile(pattern, re.IGNORECASE | re.DOTALL)
    except re.error:
        return None
    return bool(compiled.fullmatch(given.strip()))


def validate_regex_update(
    pattern: str, *, expected_literal: str, prior_given: str | None = None
) -> str | None:
    """Validate a claude-proposed regex update. Returns the pattern
    if it's safe to persist, else None.

    Checks:
      - compiles under re.IGNORECASE
      - is under MAX_REGEX_LEN
      - matches the canonical `expected_literal` answer (so accepting
        the regex doesn't accidentally reject the original right
        answer next time)
      - if `prior_given` is provided, also matches it (so the form
        the user just typed is preserved by the new regex — a
        regression sanity check on claude's output)

    Doesn't try to detect catastrophic backtracking definitively;
    that's an open problem for arbitrary regex. The MAX_REGEX_LEN
    cap + re.IGNORECASE-only flags + the fullmatch (no nested
    quantifier amplification across the whole input) are the
    pragmatic mitigations.
    """
    if not pattern or not isinstance(pattern, str):
        return None
    pattern = pattern.strip()
    if not pattern or len(pattern) > MAX_REGEX_LEN:
        return None
    try:
        compiled = re.compile(pattern, re.IGNORECASE | re.DOTALL)
    except re.error:
        return None
    if not compiled.fullmatch((expected_literal or "").strip()):
        return None
    if prior_given is not None and not compiled.fullmatch(prior_given.strip()):
        return None
    return pattern


def _grade_multi(question: dict[str, Any], user_answer: str) -> dict[str, Any]:
    """Multi-select: user_answer is a JSON array of strings, as is
    question['answer']. Order doesn't matter — set comparison."""
    try:
        picked = set(json.loads(user_answer)) if user_answer else set()
        expected = set(json.loads(question["answer"]))
    except (json.JSONDecodeError, TypeError):
        picked, expected = set(), set()
    correct = picked == expected
    return {
        "result": (Verdict.RIGHT if correct else Verdict.WRONG).value,
        "feedback": (
            "Correct."
            if correct
            else f"Expected: {sorted(expected)}; you picked: {sorted(picked)}."
        ),
        "model_answer_summary": str(sorted(expected)),
    }
