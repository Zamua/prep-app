"""Unit tests for prep.study.entities — pydantic validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from prep.study.entities import (
    CardState,
    RecentSession,
    SessionState,
    SessionStatus,
    StudySession,
)


def _session_kwargs(**overrides) -> dict:
    base = {
        "id": "abc123",
        "user_id": "alice@example.com",
        "deck_id": 1,
        "created_at": "2026-04-29T12:00:00Z",
        "last_active": "2026-04-29T12:00:00Z",
    }
    base.update(overrides)
    return base


# ---- enums ------------------------------------------------------------


def test_session_status_values():
    assert SessionStatus.ACTIVE == "active"
    assert SessionStatus.COMPLETED == "completed"
    assert SessionStatus.ABANDONED == "abandoned"


def test_session_state_values():
    assert SessionState.AWAITING_ANSWER == "awaiting-answer"
    assert SessionState.GRADING == "grading"
    assert SessionState.SHOWING_RESULT == "showing-result"


def test_session_status_rejects_unknown():
    with pytest.raises(ValueError):
        SessionStatus("paused")


# ---- StudySession -----------------------------------------------------


def test_study_session_minimal():
    s = StudySession(**_session_kwargs())
    assert s.status is SessionStatus.ACTIVE
    assert s.state is SessionState.AWAITING_ANSWER
    assert s.version == 1


def test_study_session_with_grading_state():
    s = StudySession(
        **_session_kwargs(
            state="grading",
            current_question_id=42,
            current_grading_workflow_id="grade-deck-42-abc",
        )
    )
    assert s.state is SessionState.GRADING
    assert s.current_question_id == 42


def test_study_session_rejects_empty_id():
    with pytest.raises(ValidationError):
        StudySession(**_session_kwargs(id=""))


def test_study_session_decoded_verdict_passes_through():
    s = StudySession(
        **_session_kwargs(
            last_answered_verdict={"result": "right", "feedback": "Correct."},
        )
    )
    assert s.last_answered_verdict["result"] == "right"


# ---- RecentSession ----------------------------------------------------


def test_recent_session_round_trips_dict():
    raw = {
        "id": "abc123",
        "deck_id": 1,
        "deck_name": "go-systems",
        "last_active": "2026-04-29T12:00:00Z",
        "status": "active",
        "state": "awaiting-answer",
        "device_label": "Mac",
        "current_question_id": 42,
        "current_prompt": "Implement a bounded queue.",
        "current_type": "code",
    }
    s = RecentSession.model_validate(raw)
    out = s.model_dump()
    assert out == raw


# ---- CardState --------------------------------------------------------


def test_card_state_shape():
    cs = CardState(step=2, next_due="2026-05-01T12:00:00Z", interval_minutes=4320)
    assert cs.step == 2
    assert cs.interval_minutes == 4320
