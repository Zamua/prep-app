"""Entities for the study bounded context.

The study aggregate is `StudySession` — a cross-device study attempt
on a deck, with version-checked mutations so two devices don't trample
each other's progress. Reviews are a value-object trail attached to
the session via the `study_session_answers` join.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class SessionStatus(str, Enum):
    """Lifecycle of a study session.

    - active:    user is mid-study; appears in /
    - completed: every due card answered (terminal)
    - abandoned: idle >7d or manually killed (terminal)
    """

    ACTIVE = "active"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class SessionState(str, Enum):
    """Per-card state machine within a session.

    - awaiting-answer: user is composing a response
    - grading:         answer submitted, free-text verdict is being computed
                       via the Temporal worker
    - showing-result:  verdict is back; user has not yet advanced
    """

    AWAITING_ANSWER = "awaiting-answer"
    GRADING = "grading"
    SHOWING_RESULT = "showing-result"


class StudySession(BaseModel):
    """A cross-device study attempt on a deck."""

    id: str = Field(min_length=1)
    user_id: str
    deck_id: int
    created_at: str
    last_active: str
    status: SessionStatus = SessionStatus.ACTIVE
    state: SessionState = SessionState.AWAITING_ANSWER
    current_question_id: int | None = None
    current_draft: str | None = None
    current_grading_workflow_id: str | None = None
    last_answered_qid: int | None = None
    # Verdict + state are JSON columns in sqlite. Repos decode them on
    # read, encode them on write.
    last_answered_verdict: dict[str, Any] | None = None
    last_answered_state: dict[str, Any] | None = None
    # Optimistic-concurrency guard. Every mutation bumps it; clients
    # must echo it back with each POST.
    version: int = 1
    device_label: str | None = None


class RecentSession(BaseModel):
    """A row in the home-page recent-sessions list. Joined with deck
    name + the prompt of the current card."""

    id: str
    deck_id: int
    deck_name: str
    last_active: str
    status: SessionStatus
    state: SessionState
    device_label: str | None = None
    current_question_id: int | None = None
    current_prompt: str | None = None
    current_type: str | None = None


class Review(BaseModel):
    """A single review event — append-only audit log of every grade.

    Reviews are immutable once written; SRS state lives on `cards`,
    not here. This row exists for answer-history rendering and for
    the rights/attempts aggregations the deck card view shows.
    """

    id: int
    question_id: int
    ts: str
    result: str  # 'right' | 'wrong' — see prep.domain.srs.Verdict
    user_answer: str | None = None
    grader_notes: str | None = None


class SessionAnswer(BaseModel):
    """A single answer recorded in a session — the join row that
    connects a session to the questions answered within it.

    Mostly used to know which cards the session has already seen so
    the next-card picker doesn't double-serve them.
    """

    session_id: str
    question_id: int
    answered_at: str
    result: str  # 'right' | 'wrong'
    workflow_id: str | None = None  # set if grading went via Temporal


class CardState(BaseModel):
    """The mutable SRS state on a card — step + next-due + last-review.

    Shape comes back from `record_review` so the route layer (and the
    cached `last_answered_state` JSON on a session) can render the
    "next review in N days" line without re-querying. Lives in the
    study context because reviews are what drive its mutations."""

    step: int
    next_due: str
    interval_minutes: int
