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

from prep.domain.srs import CardSRSState, Verdict, schedule_review
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
        wiring a separate reaper.

        Snoozed sessions (snoozed_until in the future) are filtered
        out — they re-appear automatically once the timestamp passes,
        no scheduler tick needed."""
        now_iso = datetime.now(timezone.utc).isoformat()
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
                   AND (s.snoozed_until IS NULL OR s.snoozed_until <= ?)
                 ORDER BY s.last_active DESC
                 LIMIT ?
                """,
                (user_id, now_iso, limit),
            ).fetchall()
        return [_row_to_recent(dict(r)) for r in rows]

    def snooze(self, user_id: str, sid: str, until_iso: str | None) -> None:
        """Hide this session from the Continue strip until `until_iso`.
        No status change — the session is still 'active', it just
        doesn't surface. Idempotent; setting twice with different
        timestamps simply overwrites. Pass an ISO-8601 UTC string,
        or None to wake the session immediately (clear the snooze)."""
        with cursor() as c:
            c.execute(
                "UPDATE study_sessions SET snoozed_until = ? WHERE id = ? AND user_id = ?",
                (until_iso, sid, user_id),
            )

    def list_snoozed(self, user_id: str) -> list[RecentSession]:
        """Sessions snoozed into the future, ordered by wake time
        (soonest first). Reuses the RecentSession shape so the index
        template can render both lists with the same partial."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with cursor() as c:
            rows = c.execute(
                """
                SELECT s.*, d.name AS deck_name,
                       q.prompt AS current_prompt, q.type AS current_type
                  FROM study_sessions s
                  JOIN decks d ON d.id = s.deck_id
                  LEFT JOIN questions q ON q.id = s.current_question_id
                 WHERE s.user_id = ? AND s.status = 'active'
                   AND s.snoozed_until IS NOT NULL AND s.snoozed_until > ?
                 ORDER BY s.snoozed_until ASC
                """,
                (user_id, now_iso),
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

    def mark_completed(self, user_id: str, sid: str) -> None:
        """Bump a session into status='completed'. Used by the session
        view route when it lands on a session whose current question
        was already answered (no due card left) — the route renders
        result.html for the just-answered card after this stamp."""
        with cursor() as c:
            c.execute(
                "UPDATE study_sessions SET status='completed', "
                "       version = version + 1, last_active = ? "
                " WHERE id = ? AND user_id = ?",
                (now(), sid, user_id),
            )

    def abandon(self, user_id: str, sid: str) -> None:
        with cursor() as c:
            c.execute(
                "UPDATE study_sessions "
                "   SET status = 'abandoned', last_active = ?, version = version + 1 "
                " WHERE id = ? AND user_id = ?",
                (now(), sid, user_id),
            )

    def abandon_all_for_deck(self, user_id: str, deck_id: int) -> int:
        """Abandon every active session this user has on the given deck.
        Returns the row count touched. Called when the deck is paused —
        leaving an in-progress session behind would let the user resume
        a deck they've explicitly silenced."""
        with cursor() as c:
            cur = c.execute(
                "UPDATE study_sessions"
                "   SET status = 'abandoned', last_active = ?, version = version + 1"
                " WHERE user_id = ? AND deck_id = ? AND status = 'active'",
                (now(), user_id, deck_id),
            )
            return cur.rowcount or 0

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
            row = c.execute(
                "SELECT step, stability, difficulty, fsrs_state, last_review "
                "FROM cards WHERE question_id = ?",
                (qid,),
            ).fetchone()
            if not row:
                raise ValueError(f"no card for question {qid}")
            last_review = None
            if row["last_review"]:
                last_review = datetime.fromisoformat(row["last_review"])
                if last_review.tzinfo is None:
                    last_review = last_review.replace(tzinfo=timezone.utc)
            state = CardSRSState(
                stability=row["stability"],
                difficulty=row["difficulty"],
                fsrs_state=row["fsrs_state"] or 1,
                last_review=last_review,
            )
            # Resolve effective FSRS desired-retention:
            #   1. deck-level override (decks.desired_retention)
            #   2. user-level default (users.desired_retention)
            #   3. algorithm default (0.90, applied inside schedule_review)
            # Single combined query — both columns are NULL by default
            # so this stays cheap even on installs that haven't picked
            # any custom retention.
            ret_row = c.execute(
                """SELECT d.desired_retention AS deck_ret,
                          u.desired_retention AS user_ret
                     FROM questions q
                     JOIN decks d ON d.id = q.deck_id
                     JOIN users u ON u.tailscale_login = q.user_id
                    WHERE q.id = ?""",
                (qid,),
            ).fetchone()
            if ret_row is not None:
                effective_retention = ret_row["deck_ret"]
                if effective_retention is None:
                    effective_retention = ret_row["user_ret"]
            else:
                effective_retention = None
            scheduled = schedule_review(
                state, verdict, now=ts, desired_retention=effective_retention
            )
            interval_minutes = max(1, scheduled.interval_seconds // 60)
            next_due_iso = scheduled.next_due.isoformat()
            c.execute(
                "INSERT INTO reviews (question_id, ts, result, user_answer, grader_notes) "
                "VALUES (?, ?, ?, ?, ?)",
                (qid, ts.isoformat(), result, user_answer, notes),
            )
            c.execute(
                """UPDATE cards
                      SET step = ?, next_due = ?, last_review = ?,
                          stability = ?, difficulty = ?, fsrs_state = ?
                    WHERE question_id = ?""",
                (
                    scheduled.step_bucket,
                    next_due_iso,
                    ts.isoformat(),
                    scheduled.state.stability,
                    scheduled.state.difficulty,
                    scheduled.state.fsrs_state,
                    qid,
                ),
            )
        return CardState(
            step=scheduled.step_bucket,
            next_due=next_due_iso,
            interval_minutes=interval_minutes,
        )

    # ---- archive (.prepdeck) restore path ------------------------------
    #
    # These three methods exist so prep.decks.archive doesn't have to
    # reach into sqlite directly: it asks the review/card repo for the
    # state of one deck (export) or pushes state back row-by-row (import).
    # The normal record() / record_grading_with_idempotency() paths run
    # the FSRS scheduler; the archive path skips it because the source
    # archive already carries the computed state.

    def list_card_state_for_deck(self, user_id: str, deck_id: int) -> list[dict]:
        """All `cards` rows for the deck, joined back to their prompt for
        the cross-reference key used in `.prepdeck` cards.csv. Returns
        plain dicts keyed by the column names so the archive codec
        doesn't need a per-shape entity for a one-off projection.
        """
        with cursor() as c:
            rows = c.execute(
                """SELECT q.prompt, c.step, c.next_due, c.last_review,
                          c.stability, c.difficulty, c.fsrs_state
                     FROM cards c JOIN questions q ON q.id = c.question_id
                    WHERE q.deck_id = ? AND q.user_id = ?""",
                (deck_id, user_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def restore_card_state(
        self,
        question_id: int,
        *,
        step: int | None = None,
        next_due: str | None = None,
        last_review: str | None = None,
        stability: float | None = None,
        difficulty: float | None = None,
        fsrs_state: int | None = None,
    ) -> None:
        """Overwrite the `cards` row's FSRS state with values from an
        external source (the archive importer). Only non-None fields
        are updated; the row's other fields are left intact.
        """
        fields: dict[str, object] = {}
        for k, v in (
            ("step", step),
            ("next_due", next_due),
            ("last_review", last_review),
            ("stability", stability),
            ("difficulty", difficulty),
            ("fsrs_state", fsrs_state),
        ):
            if v is not None:
                fields[k] = v
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        with cursor() as c:
            c.execute(
                f"UPDATE cards SET {sets} WHERE question_id = ?",
                (*fields.values(), question_id),
            )

    def list_reviews_for_deck(self, user_id: str, deck_id: int) -> list[dict]:
        """All review rows for the deck, oldest-first, joined to the
        prompt for cross-reference. Used by `.prepdeck` export."""
        with cursor() as c:
            rows = c.execute(
                """SELECT q.prompt, r.ts, r.result, r.user_answer, r.grader_notes
                     FROM reviews r JOIN questions q ON q.id = r.question_id
                    WHERE q.deck_id = ? AND q.user_id = ?
                    ORDER BY r.ts ASC, r.id ASC""",
                (deck_id, user_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def import_review(
        self,
        question_id: int,
        ts: str,
        result: str,
        user_answer: str = "",
        grader_notes: str = "",
    ) -> None:
        """Insert one review row verbatim from an external source.
        DOES NOT run the scheduler — the archive importer calls
        `restore_card_state` separately to seed the FSRS state."""
        if result not in ("right", "wrong"):
            raise ValueError(f"unknown result: {result!r}")
        with cursor() as c:
            c.execute(
                """INSERT INTO reviews (question_id, ts, result, user_answer, grader_notes)
                       VALUES (?, ?, ?, ?, ?)""",
                (question_id, ts, result, user_answer, grader_notes),
            )

    def find_idempotent_record(self, idempotency_key: str) -> dict | None:
        """Look up a previously-recorded grading by its idempotency key.
        Returns the cached SRSState dict, or None if not seen yet.

        Called by `record_grading_with_idempotency` so the Go worker
        can safely retry an activity without writing the review row
        twice."""
        with cursor() as c:
            row = c.execute(
                """SELECT step, next_due, interval_minutes
                     FROM grading_idempotency
                    WHERE idempotency_key = ?""",
                (idempotency_key,),
            ).fetchone()
        if not row:
            return None
        return {
            "step": int(row["step"]),
            "next_due": row["next_due"],
            "interval_minutes": int(row["interval_minutes"]),
        }

    def record_idempotency(self, idempotency_key: str, question_id: int, state: CardState) -> None:
        """Persist the (key → state) mapping so retries return the same
        SRSState without re-grading."""
        with cursor() as c:
            c.execute(
                """INSERT INTO grading_idempotency
                       (idempotency_key, question_id, step, next_due, interval_minutes, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    idempotency_key,
                    question_id,
                    state.step,
                    state.next_due,
                    state.interval_minutes,
                    now(),
                ),
            )

    def get_last_user_answer(self, qid: int) -> str | None:
        """Most recent user_answer recorded for `qid` across the
        reviews log. Used by the session-view route when rendering the
        showing-result page so the result template can echo back what
        the user typed. Returns None if the question has no reviews
        yet."""
        with cursor() as c:
            row = c.execute(
                "SELECT user_answer FROM reviews WHERE question_id = ? ORDER BY id DESC LIMIT 1",
                (qid,),
            ).fetchone()
        return row["user_answer"] if row else None

    def count_due_for_user(self, user_id: str) -> int:
        """Total SRS cards due-now across the user's decks that have
        notifications enabled. Paused decks excluded; trivia decks
        excluded (their cards have a `cards` row by accident of the
        schema, but they're not studied via the SRS flow + already
        have their own per-deck notifications). Used by the SRS
        notify scheduler — digest / when-ready triggers."""
        with cursor() as c:
            row = c.execute(
                """SELECT COUNT(*) AS n
                     FROM cards
                     JOIN questions ON questions.id = cards.question_id
                     JOIN decks     ON decks.id     = questions.deck_id
                    WHERE questions.user_id = ?
                      AND COALESCE(questions.suspended, 0) = 0
                      AND COALESCE(decks.notifications_enabled, 1) = 1
                      AND COALESCE(decks.deck_type, 'srs') = 'srs'
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
        snoozed_until=row.get("snoozed_until"),
    )


# ---- worker-callback helper --------------------------------------------


def record_grading_with_idempotency(
    *,
    user_id: str,
    question_id: int,
    result: str,
    user_answer: str,
    grader_notes: str,
    idempotency_key: str,
) -> dict:
    """End-to-end SRS state write with idempotency. Called by the Go
    worker via `/api/internal/record-review` after the LLM grader
    returns a verdict.

    Pre-FSRS, the Go worker did the whole thing directly: read step,
    advance via the ladder, write reviews + cards. After the FSRS swap
    the ladder math became wrong, so we route the write through Python
    where the canonical scheduler lives. The Go side keeps the
    fast LLM call and the workflow plumbing.

    Idempotency: if `idempotency_key` has been recorded before, return
    the cached state — no second insert into reviews, no double-advance
    of the SRS state. Same `grading_idempotency` table the Go worker
    used before this rewrite, so existing pin rows stay valid.
    """
    repo = ReviewRepo()
    existing = repo.find_idempotent_record(idempotency_key)
    if existing is not None:
        return existing
    new_state = repo.record(
        user_id=user_id,
        qid=question_id,
        result=result,
        user_answer=user_answer,
        notes=grader_notes,
    )
    repo.record_idempotency(idempotency_key, question_id, new_state)
    return {
        "step": new_state.step,
        "next_due": new_state.next_due,
        "interval_minutes": new_state.interval_minutes,
    }
