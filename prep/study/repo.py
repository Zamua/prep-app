"""Repositories for the study bounded context.

`SessionRepo` and `ReviewRepo` own read/write access to the
study_sessions, study_session_answers, reviews, and cards tables.
SQL lives here directly — no wrapping over prep.db.

Design notes:
- `record` is the canonical "user just graded a card" path. It writes
  a review row, advances/resets the card's step using the domain SRS
  rules, and returns the new card state.
- `record_answer_sync` / `set_grading` / `grading_completed` /
  `advance` use a session version counter — POSTs from the client
  must include the expected version; if it's stale we raise
  StaleVersionError and the client shows a "this session moved on
  another device" banner.
- All mutation methods filter on `user_id` in the WHERE clause as
  defense-in-depth — even a forgetful route can't accidentally let
  one user mutate another's session.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from prep.domain.srs import Verdict, advance_step, interval_for_step
from prep.infrastructure.db import cursor, now
from prep.study.entities import (
    CardState,
    RecentSession,
    SessionState,
    SessionStatus,
    StudySession,
)

# ---- StaleVersionError -------------------------------------------------
#
# Raised when a version-checked session mutation fails because the
# session has been advanced on another device. The route handler turns
# this into a 409 Conflict the client interprets as "show stale banner".


class StaleVersionError(Exception):
    def __init__(self, current_version: int):
        super().__init__(f"stale session version (current is {current_version})")
        self.current_version = current_version


def _maybe_decode_json(v: Any) -> Any:
    """Coerce a JSON-encoded string into the python value, pass dicts /
    None through. Used at the row-to-entity boundary for sqlite TEXT
    columns we store JSON in (last_answered_verdict, last_answered_state
    on study_sessions)."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return None
    return v


def _new_session_id() -> str:
    return uuid.uuid4().hex[:16]


class SessionRepo:
    """Read/write access to study_sessions + study_session_answers."""

    # ---- creation --------------------------------------------------

    def create(self, user_id: str, deck_id: int, device_label: str) -> str:
        """Start a fresh session. Returns the new session id. Picks
        the first due card and seeds current_draft from the question's
        skeleton (if any)."""
        ts = now()
        sid = _new_session_id()
        next_q = self._pick_next_question(user_id, deck_id, sid)
        initial_draft = (next_q.get("skeleton") or "") if next_q else ""
        with cursor() as c:
            c.execute(
                """
                INSERT INTO study_sessions
                    (id, user_id, deck_id, created_at, last_active, status, state,
                     current_question_id, current_draft, version, device_label)
                VALUES (?, ?, ?, ?, ?, 'active', 'awaiting-answer', ?, ?, 1, ?)
                """,
                (
                    sid,
                    user_id,
                    deck_id,
                    ts,
                    ts,
                    next_q["id"] if next_q else None,
                    initial_draft,
                    device_label,
                ),
            )
        return sid

    def device_label_from_ua(self, ua: str | None) -> str:
        """Light-touch UA sniffing for the recent-sessions list."""
        if not ua:
            return "unknown device"
        ua = ua.lower()
        if "ipad" in ua:
            return "iPad"
        if "iphone" in ua:
            return "iPhone"
        if "mac os x" in ua or "macintosh" in ua:
            return "Mac"
        if "android" in ua:
            return "Android"
        if "windows" in ua:
            return "Windows"
        if "linux" in ua:
            return "Linux"
        return "browser"

    # ---- reads -----------------------------------------------------

    def get(self, user_id: str, sid: str) -> StudySession | None:
        with cursor() as c:
            row = c.execute(
                "SELECT * FROM study_sessions WHERE id = ? AND user_id = ?",
                (sid, user_id),
            ).fetchone()
        if not row:
            return None
        return _row_to_session(dict(row))

    def find_active_for_deck(self, user_id: str, deck_id: int) -> StudySession | None:
        with cursor() as c:
            row = c.execute(
                "SELECT * FROM study_sessions "
                " WHERE deck_id = ? AND user_id = ? AND status = 'active' "
                " ORDER BY last_active DESC LIMIT 1",
                (deck_id, user_id),
            ).fetchone()
        if not row:
            return None
        return _row_to_session(dict(row))

    def list_recent(self, user_id: str, limit: int = 5) -> list[RecentSession]:
        """Recent active sessions for this user across all their decks.

        Side-effect: ages out THIS USER's sessions idle for >7d into
        status='abandoned'. Cheap to do on each list call rather than
        wiring a separate reaper."""
        abandon_before = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        with cursor() as c:
            c.execute(
                "UPDATE study_sessions SET status = 'abandoned' "
                " WHERE user_id = ? AND status = 'active' AND last_active < ?",
                (user_id, abandon_before),
            )
            rows = c.execute(
                """
                SELECT s.*, d.name AS deck_name,
                       q.prompt AS current_prompt, q.type AS current_type
                  FROM study_sessions s
                  JOIN decks d ON d.id = s.deck_id
                  LEFT JOIN questions q ON q.id = s.current_question_id
                 WHERE s.user_id = ? AND s.status = 'active'
                 ORDER BY s.last_active DESC
                 LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [_row_to_recent(dict(r)) for r in rows]

    # ---- mutations -------------------------------------------------

    def update_draft(self, user_id: str, sid: str, draft: str, expected_version: int) -> int:
        """Save the in-progress draft. Version-checked. Returns new
        version. Raises StaleVersionError on conflict."""
        ts = now()
        with cursor() as c:
            row = c.execute(
                "SELECT version FROM study_sessions WHERE id = ? AND user_id = ?",
                (sid, user_id),
            ).fetchone()
            if not row:
                raise ValueError(f"session {sid} not found for user")
            if row["version"] != expected_version:
                raise StaleVersionError(row["version"])
            new_v = expected_version + 1
            c.execute(
                "UPDATE study_sessions "
                "   SET current_draft = ?, last_active = ?, version = ? "
                " WHERE id = ? AND user_id = ?",
                (draft, ts, new_v, sid, user_id),
            )
            return new_v

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
        """Synchronous answer recording (mcq/multi). Records the
        answer, sets state='showing-result', stores cached
        verdict/state. Bumps version. Returns new version."""
        ts = now()
        with cursor() as c:
            row = c.execute(
                "SELECT version FROM study_sessions WHERE id = ? AND user_id = ?",
                (sid, user_id),
            ).fetchone()
            if not row:
                raise ValueError(f"session {sid} not found for user")
            if row["version"] != expected_version:
                raise StaleVersionError(row["version"])
            new_v = expected_version + 1
            # Record in session_answers (idempotent via PK).
            c.execute(
                "INSERT OR REPLACE INTO study_session_answers "
                " (session_id, question_id, answered_at, result, workflow_id) "
                "VALUES (?, ?, ?, ?, NULL)",
                (sid, question_id, ts, verdict["result"]),
            )
            c.execute(
                """UPDATE study_sessions SET
                    state = 'showing-result',
                    current_draft = NULL,
                    last_answered_qid = ?,
                    last_answered_verdict = ?,
                    last_answered_state = ?,
                    last_active = ?,
                    version = ?
                  WHERE id = ? AND user_id = ?""",
                (question_id, json.dumps(verdict), json.dumps(state), ts, new_v, sid, user_id),
            )
            return new_v

    def set_grading(
        self,
        user_id: str,
        sid: str,
        question_id: int,
        workflow_id: str,
        expected_version: int,
    ) -> int:
        """Used when a code/short submission kicks off a grading
        workflow. Sets state='grading', stores the workflow id,
        version-checked."""
        ts = now()
        with cursor() as c:
            row = c.execute(
                "SELECT version FROM study_sessions WHERE id = ? AND user_id = ?",
                (sid, user_id),
            ).fetchone()
            if not row:
                raise ValueError(f"session {sid} not found for user")
            if row["version"] != expected_version:
                raise StaleVersionError(row["version"])
            new_v = expected_version + 1
            c.execute(
                """UPDATE study_sessions SET
                    state = 'grading',
                    current_grading_workflow_id = ?,
                    last_active = ?,
                    version = ?
                  WHERE id = ? AND user_id = ?""",
                (workflow_id, ts, new_v, sid, user_id),
            )
            return new_v

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
        ts = now()
        with cursor() as c:
            row = c.execute(
                "SELECT state, version FROM study_sessions WHERE id = ? AND user_id = ?",
                (sid, user_id),
            ).fetchone()
            if not row:
                return
            # Idempotent: only act if we're still in grading state.
            if row["state"] != "grading":
                return
            c.execute(
                "INSERT OR REPLACE INTO study_session_answers "
                " (session_id, question_id, answered_at, result, workflow_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (sid, question_id, ts, verdict["result"], workflow_id),
            )
            c.execute(
                """UPDATE study_sessions SET
                    state = 'showing-result',
                    current_grading_workflow_id = NULL,
                    current_draft = NULL,
                    last_answered_qid = ?,
                    last_answered_verdict = ?,
                    last_answered_state = ?,
                    last_active = ?,
                    version = version + 1
                  WHERE id = ? AND user_id = ?""",
                (question_id, json.dumps(verdict), json.dumps(state), ts, sid, user_id),
            )

    def advance(self, user_id: str, sid: str, expected_version: int) -> int:
        """Move from showing-result to the next due card (or
        completed). Returns new version."""
        ts = now()
        with cursor() as c:
            row = c.execute(
                "SELECT version, deck_id FROM study_sessions WHERE id = ? AND user_id = ?",
                (sid, user_id),
            ).fetchone()
            if not row:
                raise ValueError(f"session {sid} not found for user")
            if row["version"] != expected_version:
                raise StaleVersionError(row["version"])
            next_q = self._pick_next_question(user_id, row["deck_id"], sid)
            new_v = expected_version + 1
            if next_q is None:
                c.execute(
                    """UPDATE study_sessions SET
                        status = 'completed',
                        state = 'awaiting-answer',
                        current_question_id = NULL,
                        current_draft = NULL,
                        last_answered_qid = NULL,
                        last_answered_verdict = NULL,
                        last_answered_state = NULL,
                        last_active = ?, version = ?
                      WHERE id = ? AND user_id = ?""",
                    (ts, new_v, sid, user_id),
                )
            else:
                c.execute(
                    """UPDATE study_sessions SET
                        state = 'awaiting-answer',
                        current_question_id = ?,
                        current_draft = ?,
                        last_answered_qid = NULL,
                        last_answered_verdict = NULL,
                        last_answered_state = NULL,
                        last_active = ?, version = ?
                      WHERE id = ? AND user_id = ?""",
                    (next_q["id"], next_q.get("skeleton") or "", ts, new_v, sid, user_id),
                )
            return new_v

    def abandon(self, user_id: str, sid: str) -> None:
        with cursor() as c:
            c.execute(
                "UPDATE study_sessions "
                "   SET status = 'abandoned', last_active = ?, version = version + 1 "
                " WHERE id = ? AND user_id = ?",
                (now(), sid, user_id),
            )

    # ---- internal --------------------------------------------------

    def _pick_next_question(self, user_id: str, deck_id: int, sid: str) -> dict | None:
        """Return the next question this session should show: a card
        that's due AND hasn't been answered in this session yet. Used
        by `create` (initial card) and `advance` (next card after a
        grade). Re-fetches the question via QuestionRepo so the
        skeleton seed comes through."""
        from prep.decks.repo import QuestionRepo

        ts = now()
        with cursor() as c:
            row = c.execute(
                """
                SELECT q.id
                  FROM questions q
                  JOIN cards ON cards.question_id = q.id
                 WHERE q.deck_id = ? AND q.user_id = ?
                   AND COALESCE(q.suspended, 0) = 0
                   AND cards.next_due <= ?
                   AND q.id NOT IN (
                       SELECT question_id FROM study_session_answers WHERE session_id = ?
                   )
                 ORDER BY cards.next_due ASC
                 LIMIT 1
                """,
                (deck_id, user_id, ts, sid),
            ).fetchone()
        if not row:
            return None
        # Return the question as a dict with skeleton to feed
        # current_draft seeding. QuestionRepo.get returns a Question
        # entity; convert to a thin dict.
        q = QuestionRepo().get(user_id, row["id"])
        if q is None:
            return None
        return {"id": q.id, "skeleton": q.skeleton}


class ReviewRepo:
    """Write access to reviews + the SRS state on cards."""

    def record(
        self,
        user_id: str,
        qid: int,
        result: str,
        user_answer: str,
        notes: str = "",
    ) -> CardState:
        """Record a review and advance/reset the SRS step.

        Verifies the question belongs to the user before mutating SRS
        state — defense in depth in case a route misses the check.
        Returns the new card state."""
        try:
            verdict = Verdict(result)
        except ValueError as e:
            raise ValueError(f"unknown result: {result}") from e
        ts = datetime.now(timezone.utc)
        with cursor() as c:
            owner = c.execute("SELECT user_id FROM questions WHERE id = ?", (qid,)).fetchone()
            if not owner or owner["user_id"] != user_id:
                raise ValueError(f"question {qid} not owned by {user_id}")
            row = c.execute("SELECT step FROM cards WHERE question_id = ?", (qid,)).fetchone()
            if not row:
                raise ValueError(f"no card for question {qid}")
            step = row["step"]
            new_step = advance_step(step, verdict)
            interval_td = interval_for_step(new_step)
            interval = int(interval_td.total_seconds() // 60)
            next_due = (ts + interval_td).isoformat()
            c.execute(
                "INSERT INTO reviews (question_id, ts, result, user_answer, grader_notes) "
                "VALUES (?, ?, ?, ?, ?)",
                (qid, ts.isoformat(), result, user_answer, notes),
            )
            c.execute(
                "UPDATE cards SET step = ?, next_due = ?, last_review = ? WHERE question_id = ?",
                (new_step, next_due, ts.isoformat(), qid),
            )
        return CardState(step=new_step, next_due=next_due, interval_minutes=interval)

    def count_due_for_user(self, user_id: str) -> int:
        """Total cards due-now across the user's decks that have
        notifications enabled. Paused decks are excluded — if 90 cards
        are due overall but 30 are in paused decks, this returns 60.
        The notify scheduler uses this to decide whether to send a
        digest / threshold ping."""
        with cursor() as c:
            row = c.execute(
                """SELECT COUNT(*) AS n
                     FROM cards
                     JOIN questions ON questions.id = cards.question_id
                     JOIN decks     ON decks.id     = questions.deck_id
                    WHERE questions.user_id = ?
                      AND COALESCE(questions.suspended, 0) = 0
                      AND COALESCE(decks.notifications_enabled, 1) = 1
                      AND cards.next_due <= ?""",
                (user_id, now()),
            ).fetchone()
        return int(row["n"]) if row else 0

    def due_questions(self, user_id: str, deck_id: int, limit: int = 3) -> list[dict]:
        """Cards due now, oldest-due first. Returns full Question-shaped
        dicts so the legacy /study route can render them without an
        extra round-trip per id."""
        from prep.decks.repo import QuestionRepo

        ts = now()
        with cursor() as c:
            rows = c.execute(
                """
                SELECT q.id
                  FROM questions q
                  JOIN cards ON cards.question_id = q.id
                 WHERE q.deck_id = ? AND q.user_id = ?
                   AND COALESCE(q.suspended, 0) = 0
                   AND cards.next_due <= ?
                 ORDER BY cards.next_due ASC
                 LIMIT ?
                """,
                (deck_id, user_id, ts, limit),
            ).fetchall()
        qrepo = QuestionRepo()
        out: list[dict] = []
        for r in rows:
            q = qrepo.get(user_id, r["id"])
            if q is None:
                continue
            d = q.model_dump()
            # Legacy callers expect choices_list at the dict level.
            d["choices_list"] = list(q.choices) if q.choices else []
            out.append(d)
        return out


# ---- row-to-entity helpers ----------------------------------------------


def _row_to_session(row: dict) -> StudySession:
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
