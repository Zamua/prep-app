"""Repositories for the study bounded context.

`SessionRepo` and `ReviewRepo` own read/write access to the
study_sessions, study_session_answers, reviews, and cards tables.

Like the decks repos in phase 5b, these are facades over the
existing prep.db accessors for now — a later phase will pull the
SQL into this module once every caller has switched to the
entity-typed surface.

Most of the cross-table interactions (record_review touches both
reviews + cards; create_session reads cards/questions to seed the
draft) stay in prep.db for now; the repo just exposes them with
entity types at the boundary.
"""

from __future__ import annotations

import json
from typing import Any

from prep import db as _legacy_db
from prep.study.entities import (
    CardState,
    RecentSession,
    SessionState,
    SessionStatus,
    StudySession,
)


def _maybe_decode_json(v: Any) -> Any:
    """Coerce a JSON-encoded string into the python value, pass dicts /
    None through. Used at the row-to-entity boundary for sqlite TEXT
    columns we store JSON in (last_answered_verdict, last_answered_state
    on study_sessions).

    The legacy `prep.db.get_session` decodes these inline; legacy
    `prep.db.find_active_session_for_deck` does NOT — that asymmetry
    was invisible until phase-6 entity validation made it crash. This
    helper centralizes the decode at the entity-construction step so
    BOTH read paths end up with a dict (or None on un-parseable bytes).
    """
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return None
    return v


# Re-export StaleVersionError so callers in study/ don't need to dip
# into prep.db directly.
StaleVersionError = _legacy_db.StaleVersionError


class SessionRepo:
    """Read/write access to study_sessions + study_session_answers."""

    # ---- creation --------------------------------------------------

    def create(self, user_id: str, deck_id: int, device_label: str) -> str:
        """Start a fresh session. Returns the new session id. Picks
        the first due card and seeds current_draft from the question's
        skeleton (if any)."""
        return _legacy_db.create_session(user_id, deck_id, device_label)

    def device_label_from_ua(self, ua: str | None) -> str:
        """Light-touch UA sniffing for the recent-sessions list. Lives
        on the repo because it's purely about how a session row is
        labeled — not domain logic."""
        return _legacy_db.device_label_from_ua(ua)

    # ---- reads -----------------------------------------------------

    def get(self, user_id: str, sid: str) -> StudySession | None:
        row = _legacy_db.get_session(user_id, sid)
        if row is None:
            return None
        return _row_to_session(row)

    def find_active_for_deck(self, user_id: str, deck_id: int) -> StudySession | None:
        row = _legacy_db.find_active_session_for_deck(user_id, deck_id)
        if row is None:
            return None
        return _row_to_session(row)

    def list_recent(self, user_id: str, limit: int = 5) -> list[RecentSession]:
        rows = _legacy_db.list_recent_sessions(user_id, limit)
        return [_row_to_recent(r) for r in rows]

    # ---- mutations -------------------------------------------------

    def update_draft(self, user_id: str, sid: str, draft: str, expected_version: int) -> int:
        """Save the in-progress draft. Version-checked. Returns new version.
        Raises StaleVersionError on conflict."""
        return _legacy_db.update_session_draft(user_id, sid, draft, expected_version)

    def record_answer_sync(
        self,
        user_id: str,
        sid: str,
        question_id: int,
        expected_version: int,
        user_answer: str,
        verdict: dict,
        state: dict,
    ) -> int:
        return _legacy_db.record_session_answer_sync(
            user_id, sid, question_id, expected_version, user_answer, verdict, state
        )

    def set_grading(
        self,
        user_id: str,
        sid: str,
        question_id: int,
        workflow_id: str,
        expected_version: int,
    ) -> int:
        """Mark the session as 'grading' (waiting on a Temporal
        workflow). Version-checked. Returns new version."""
        return _legacy_db.set_session_grading(
            user_id, sid, question_id, workflow_id, expected_version
        )

    def grading_completed(
        self,
        user_id: str,
        sid: str,
        question_id: int,
        verdict: dict,
        state: dict,
        workflow_id: str,
    ) -> None:
        """Reconciliation path: a polling route saw the grading
        workflow finish, so we stamp the answer and flip to
        showing-result. Not version-checked (server-side, not user
        input). Idempotent — second call is a no-op."""
        _legacy_db.session_grading_completed(user_id, sid, question_id, verdict, state, workflow_id)

    def advance(self, user_id: str, sid: str, expected_version: int) -> int:
        """Advance the session to the next card. Returns new version.
        Raises StaleVersionError on conflict; if no more cards are due,
        the session transitions to `completed`."""
        return _legacy_db.advance_session(user_id, sid, expected_version)

    def abandon(self, user_id: str, sid: str) -> None:
        _legacy_db.abandon_session(user_id, sid)


class ReviewRepo:
    """Write access to reviews + the SRS state on cards.

    `record` is the canonical "user just graded a card" path. It
    writes a review row, advances/resets the card's step using the
    domain SRS rules, and returns the new card state."""

    def record(
        self,
        user_id: str,
        qid: int,
        result: str,
        user_answer: str,
        notes: str = "",
    ) -> CardState:
        raw = _legacy_db.record_review(user_id, qid, result, user_answer, notes)
        return CardState(
            step=raw["step"],
            next_due=raw["next_due"],
            interval_minutes=raw["interval_minutes"],
        )

    def count_due_for_user(self, user_id: str) -> int:
        return _legacy_db.count_due_for_user(user_id)


# ---- row-to-entity helpers ----------------------------------------------


def _row_to_session(row: dict) -> StudySession:
    """Decode a study_sessions row into a StudySession entity."""
    return StudySession(
        id=row["id"],
        user_id=row["user_id"],
        deck_id=row["deck_id"],
        created_at=row["created_at"],
        last_active=row["last_active"],
        status=SessionStatus(row.get("status") or "active"),
        state=SessionState(row.get("state") or "awaiting-answer"),
        current_question_id=row.get("current_question_id"),
        current_draft=row.get("current_draft"),
        current_grading_workflow_id=row.get("current_grading_workflow_id"),
        last_answered_qid=row.get("last_answered_qid"),
        last_answered_verdict=_maybe_decode_json(row.get("last_answered_verdict")),
        last_answered_state=_maybe_decode_json(row.get("last_answered_state")),
        version=row.get("version") or 1,
        device_label=row.get("device_label"),
    )


def _row_to_recent(row: dict) -> RecentSession:
    """Decode a list_recent_sessions row into a RecentSession entity."""
    return RecentSession(
        id=row["id"],
        deck_id=row["deck_id"],
        deck_name=row["deck_name"],
        last_active=row["last_active"],
        status=SessionStatus(row.get("status") or "active"),
        state=SessionState(row.get("state") or "awaiting-answer"),
        device_label=row.get("device_label"),
        current_question_id=row.get("current_question_id"),
        current_prompt=row.get("current_prompt"),
        current_type=row.get("current_type"),
    )
