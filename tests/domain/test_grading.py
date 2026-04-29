"""Domain tests for the synchronous grader.

mcq + multi + idk paths only — free-text grading goes through
Temporal and isn't part of this module.
"""

from __future__ import annotations

import json

import pytest

from prep.domain.grading import grade


def _mcq(answer: str) -> dict:
    return {"type": "mcq", "answer": answer}


def _multi(*answers: str) -> dict:
    return {"type": "multi", "answer": json.dumps(list(answers))}


# ---- mcq ---------------------------------------------------------------


def test_mcq_correct_returns_right():
    result = grade(_mcq("A"), "A")
    assert result["result"] == "right"
    assert result["feedback"] == "Correct."
    assert result["model_answer_summary"] == "A"


def test_mcq_wrong_returns_wrong():
    result = grade(_mcq("A"), "B")
    assert result["result"] == "wrong"
    assert result["feedback"] == "Wrong choice."
    assert result["model_answer_summary"] == "A"


def test_mcq_strips_whitespace():
    result = grade(_mcq("  A  "), "A")
    assert result["result"] == "right"


def test_mcq_empty_answer_against_empty_correct():
    """Edge case: empty user_answer + empty correct = match. Probably
    impossible in practice, but pinning the behavior."""
    result = grade(_mcq(""), "")
    assert result["result"] == "right"


# ---- multi -------------------------------------------------------------


def test_multi_exact_match_returns_right():
    result = grade(_multi("A", "B"), json.dumps(["A", "B"]))
    assert result["result"] == "right"


def test_multi_order_independent():
    """Sets, not lists — order shouldn't matter."""
    result = grade(_multi("A", "B"), json.dumps(["B", "A"]))
    assert result["result"] == "right"


def test_multi_missing_choice_is_wrong():
    result = grade(_multi("A", "B"), json.dumps(["A"]))
    assert result["result"] == "wrong"
    assert "Expected:" in result["feedback"]


def test_multi_extra_choice_is_wrong():
    result = grade(_multi("A"), json.dumps(["A", "B"]))
    assert result["result"] == "wrong"


def test_multi_invalid_json_does_not_500():
    """Defense in depth — malformed payload doesn't crash the request.

    Note: the current grader resets BOTH picked and expected to empty
    on JSONDecodeError, so result == 'right' rather than 'wrong'.
    That's a latent bug worth fixing under the study bounded context
    (Phase 6); this test pins the current behavior so we notice if
    something changes it before then.
    """
    result = grade(_multi("A"), "not-json{{")
    assert result["result"] == "right"  # arguably wrong, but current behavior


def test_multi_empty_user_answer_treated_as_empty_set():
    result = grade(_multi("A"), "")
    assert result["result"] == "wrong"


# ---- idk ---------------------------------------------------------------


def test_idk_always_wrong():
    """An 'I don't know' submission counts as wrong, regardless of
    type or actual answer — caller passes idk=True before any
    type-specific logic runs."""
    result = grade(_mcq("A"), "A", idk=True)
    assert result["result"] == "wrong"
    assert "I don't know" in result["feedback"]


def test_idk_truncates_long_answer_summary():
    """The model_answer_summary slot in the idk path slices to 400 chars."""
    long = "x" * 1000
    result = grade({"type": "mcq", "answer": long}, "ignored", idk=True)
    assert len(result["model_answer_summary"]) == 400


def test_idk_with_no_answer_field():
    """A question without an 'answer' field shouldn't crash the idk path."""
    result = grade({"type": "mcq"}, "", idk=True)
    assert result["result"] == "wrong"
    assert result["model_answer_summary"] == ""


# ---- free-text rejection ----------------------------------------------


@pytest.mark.parametrize("qtype", ["short", "code", "freetext"])
def test_free_text_types_raise_value_error(qtype: str):
    """Free-text grading goes through Temporal; calling this sync
    helper with one of those types is a programming error."""
    with pytest.raises(ValueError, match="Temporal worker"):
        grade({"type": qtype, "answer": "ignored"}, "ignored")
