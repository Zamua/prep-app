"""Application services / use cases for the study bounded context.

Mirrors the shape of prep.decks.service: plain functions, take
dependencies as parameters, route layer translates HTTP to a
service call and back.

Two flavors here:
1. Synchronous CRUD use cases — start_session, submit_self_grade,
   advance, abandon, list_recent. each is one repo write or read.
2. Async orchestration — start_grading + grading-progress lookups,
   for the free-text grading path that runs through Temporal.
"""

from __future__ import annotations

from typing import Any

from prep.study.entities import (
    CardState,
    RecentSession,
    StudySession,
)
from prep.study.repo import ReviewRepo, SessionRepo

# ============================================================================
# Synchronous CRUD use cases
# ============================================================================


def start_session(
    repo: SessionRepo,
    user_id: str,
    deck_id: int,
    device_label: str,
) -> str:
    """Create a fresh study session. Returns the new session id."""
    return repo.create(user_id, deck_id, device_label)


def get_session(repo: SessionRepo, user_id: str, sid: str) -> StudySession | None:
    return repo.get(user_id, sid)


def find_active_session(repo: SessionRepo, user_id: str, deck_id: int) -> StudySession | None:
    """Used by the auto-resume path: hitting /study/{deck}/begin returns
    an existing active session if one is open, otherwise creates a new
    one. Returning None is the caller's signal to create."""
    return repo.find_active_for_deck(user_id, deck_id)


def list_recent_sessions(repo: SessionRepo, user_id: str, limit: int = 5) -> list[RecentSession]:
    return repo.list_recent(user_id, limit)


def update_draft(
    repo: SessionRepo, user_id: str, sid: str, draft: str, expected_version: int
) -> int:
    """Save the in-progress draft. Version-checked. Returns new version.
    Raises StaleVersionError on conflict."""
    return repo.update_draft(user_id, sid, draft, expected_version)


def advance_session(repo: SessionRepo, user_id: str, sid: str, expected_version: int) -> int:
    """Pick the next due card and advance the session. If no more due
    cards, the session transitions to `completed`. Version-checked."""
    return repo.advance(user_id, sid, expected_version)


def abandon_session(repo: SessionRepo, user_id: str, sid: str) -> None:
    repo.abandon(user_id, sid)


def submit_sync_answer(
    session_repo: SessionRepo,
    review_repo: ReviewRepo,
    *,
    user_id: str,
    sid: str,
    qid: int,
    expected_version: int,
    user_answer: str,
    verdict: dict,
) -> tuple[CardState, int]:
    """Complete a deterministic-grade answer (mcq/multi/idk) in one
    shot: record the review, then mark the session row with the
    cached verdict + state. Returns (card_state, new_session_version).
    Raises StaleVersionError if the session has moved."""
    state = review_repo.record(
        user_id, qid, verdict["result"], user_answer, notes=verdict.get("feedback") or ""
    )
    new_v = session_repo.record_answer_sync(
        user_id,
        sid,
        qid,
        expected_version,
        user_answer,
        verdict,
        state.model_dump(),
    )
    return state, new_v


# ============================================================================
# Async orchestration — free-text grading via Temporal
# ============================================================================
#
# The free-text grading path differs from sync: the user's answer goes
# to the Temporal worker for an LLM judge, the route returns
# immediately with state='grading', and the user polls /grading/<wid>
# until the workflow lands a verdict.


async def start_grading(
    client: Any,
    session_repo: SessionRepo,
    *,
    user_id: str,
    sid: str,
    qid: int,
    deck_name: str,
    expected_version: int,
    user_answer: str,
    idk: bool = False,
) -> str:
    """Kick off a GradeAnswer workflow + mark the session row as
    'grading'. Returns the workflow id. Raises StaleVersionError if
    the session moved before we could mark it grading.

    The temporal_client.start_grading signature is
    (qid, deck_name, user_answer, idk, *, user_id), so we mirror it
    here verbatim — adapter, not domain remodeling."""
    result = await client.start_grading(qid, deck_name, user_answer, idk, user_id=user_id)
    session_repo.set_grading(user_id, sid, qid, result.workflow_id, expected_version)
    return result.workflow_id


async def get_grading_progress(client: Any, wid: str) -> dict:
    return await client.get_grade_progress(wid)


async def get_grading_result(client: Any, wid: str) -> dict:
    return await client.get_grade_result(wid)


def grading_landed(
    repo: SessionRepo,
    *,
    user_id: str,
    sid: str,
    question_id: int,
    workflow_id: str,
    verdict: dict,
    state: dict,
) -> None:
    """Called by the route after polling sees a terminal grading state.
    Records the cached verdict + state on the session row. Idempotent:
    second call once we're already in showing-result is a no-op."""
    repo.grading_completed(user_id, sid, question_id, verdict, state, workflow_id)
