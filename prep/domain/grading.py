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
"""

from __future__ import annotations

import json
from typing import Any

from prep.domain.srs import Verdict


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
