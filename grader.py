"""Synchronous grader for the deterministic question types.

Used by the FastAPI request thread for `mcq`, `multi`, and "I don't know"
submissions — these resolve in microseconds and don't need a workflow.
Free-text grading (`code` / `short` answers requiring an LLM judge) is
NOT here; that path goes through the Temporal worker so the request
doesn't block on a multi-second claude call. See:

  - app.py: study_submit / session_submit dispatch the slow path to
    temporal_client.start_grading
  - worker-go/activities/grading.go: the GradeFreeText activity
"""

from __future__ import annotations

import json


def grade(question: dict, user_answer: str, idk: bool = False) -> dict:
    if idk:
        return {
            "result": "wrong",
            "feedback": "Marked as 'I don't know' — see again soon.",
            "model_answer_summary": question.get("answer", "")[:400],
        }
    qtype = question["type"]
    if qtype == "mcq":
        correct = (user_answer or "").strip() == (question["answer"] or "").strip()
        return {
            "result": "right" if correct else "wrong",
            "feedback": "Correct." if correct else "Wrong choice.",
            "model_answer_summary": question["answer"],
        }
    if qtype == "multi":
        try:
            picked = set(json.loads(user_answer)) if user_answer else set()
            expected = set(json.loads(question["answer"]))
        except (json.JSONDecodeError, TypeError):
            picked, expected = set(), set()
        correct = picked == expected
        return {
            "result": "right" if correct else "wrong",
            "feedback": "Correct." if correct else f"Expected: {sorted(expected)}; you picked: {sorted(picked)}.",
            "model_answer_summary": str(sorted(expected)),
        }
    raise ValueError(
        f"grader.grade() called with type={qtype!r}; free-text grading "
        f"goes through the Temporal worker (GradeFreeText activity), not "
        f"this synchronous helper."
    )
