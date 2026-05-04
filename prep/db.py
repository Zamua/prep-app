"""SQLite layer for prep — a self-hosted SRS flashcard tool.

Schema:
  users       : one row per Tailscale identity (login email is the PK)
  decks       : one row per (user, name) — names are NOT globally unique
  questions   : prompt + type + answer key + rubric, scoped to a user
  reviews     : every grading event, scoped via question → user
  cards       : one-to-one with questions, holds current SRS state
  study_sessions / study_session_answers : cross-device study state, per-user

Multi-user model:
  • Every user-owned table has a `user_id` column = users.tailscale_login.
  • Every read filters WHERE user_id = ?. Every write stamps user_id.
  • Auth: Tailscale identity passthrough (Tailscale-User-Login header) when
    available; falls back to PREP_DEFAULT_USER env var for development /
    single-user setups. See app._auth_user.

SRS = simplified SM-2:
  wrong              -> next interval = 10 minutes,  ease unchanged
  right (first time) -> 1d
  right after 1d     -> 3d
  right after 3d     -> 7d
  right after 7d     -> 14d
  right after 14d    -> 30d
  beyond 30d         -> stays at 30d
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from prep.domain.srs import LADDER_MINUTES, Verdict, advance_step, interval_for_step

# Connection layer — see prep/infrastructure/db.py. Re-exported here so
# the dozens of `db.cursor()` / `db.init()` / `db.now()` call sites
# don't have to change in this phase. Subsequent phases (5+) will move
# the per-context accessors below into bounded-context repo modules
# that import from prep.infrastructure.db directly.
from prep.infrastructure.db import DB_PATH, cursor, init, now

__all__ = [
    # Connection re-exports.
    "DB_PATH",
    "cursor",
    "init",
    "now",
    # Domain re-exports kept for backwards compat — prefer importing
    # from prep.domain.srs in new code.
    "INTERVAL_LADDER_MINUTES",
    "QUESTION_TYPES",
]

# Backwards-compatible alias for older code paths that imported the
# ladder directly. Prefer `from prep.domain.srs import LADDER_MINUTES`
# in new code.
INTERVAL_LADDER_MINUTES = list(LADDER_MINUTES)

QUESTION_TYPES = {"code", "mcq", "multi", "short"}


# =============================================================================
# Decks (per-user)
# =============================================================================


def get_or_create_deck(user_id: str, name: str) -> int:
    with cursor() as c:
        row = c.execute(
            "SELECT id FROM decks WHERE user_id = ? AND name = ?",
            (user_id, name),
        ).fetchone()
        if row:
            return row["id"]
        cur = c.execute(
            "INSERT INTO decks (user_id, name, created_at) VALUES (?, ?, ?)",
            (user_id, name, now()),
        )
        return cur.lastrowid


def find_deck(user_id: str, name: str) -> int | None:
    """Read-only deck lookup — does not auto-create. Used for ownership
    checks on workflow status routes where we must NOT side-effect on a
    misrouted poll."""
    with cursor() as c:
        row = c.execute(
            "SELECT id FROM decks WHERE user_id = ? AND name = ?",
            (user_id, name),
        ).fetchone()
        return row["id"] if row else None


def create_deck(user_id: str, name: str, context_prompt: str | None = None) -> int:
    """Insert a new deck row. Caller is responsible for validating the name
    (alphanumeric + hyphens, length cap, etc.). Raises sqlite3.IntegrityError
    if the (user_id, name) pair already exists."""
    with cursor() as c:
        cur = c.execute(
            "INSERT INTO decks (user_id, name, created_at, context_prompt) VALUES (?, ?, ?, ?)",
            (user_id, name, now(), context_prompt),
        )
        return cur.lastrowid


def get_deck_context_prompt(user_id: str, name: str) -> str | None:
    """Returns the user-supplied context prompt for a deck, or None if the
    deck doesn't exist or has no prompt set yet (legacy decks, or a row
    pre-dating UI creation)."""
    with cursor() as c:
        row = c.execute(
            "SELECT context_prompt FROM decks WHERE user_id = ? AND name = ?",
            (user_id, name),
        ).fetchone()
    if not row:
        return None
    return row["context_prompt"]


def update_deck_context_prompt(user_id: str, name: str, context_prompt: str) -> None:
    with cursor() as c:
        c.execute(
            "UPDATE decks SET context_prompt = ? WHERE user_id = ? AND name = ?",
            (context_prompt, user_id, name),
        )


def delete_deck(user_id: str, name: str) -> int:
    """Delete a deck by name and return the count of rows removed (0 or 1).
    FK CASCADE removes the deck's questions; question CASCADEs remove
    cards / reviews / study_session_answers. study_sessions on the deck
    also cascade. So a single DELETE wipes the entire subtree."""
    with cursor() as c:
        cur = c.execute(
            "DELETE FROM decks WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
        return cur.rowcount


def list_decks(user_id: str) -> list[dict]:
    with cursor() as c:
        rows = c.execute(
            """
            SELECT d.id, d.name, d.deck_type,
                   COUNT(q.id) AS total,
                   SUM(CASE WHEN cards.next_due <= ? AND COALESCE(q.suspended,0)=0
                            THEN 1 ELSE 0 END) AS due
              FROM decks d
              LEFT JOIN questions q ON q.deck_id = d.id AND q.user_id = d.user_id
              LEFT JOIN cards ON cards.question_id = q.id
             WHERE d.user_id = ?
             GROUP BY d.id
             ORDER BY d.name
            """,
            (now(), user_id),
        ).fetchall()
        return [dict(r) for r in rows]


def add_question(
    user_id: str,
    deck_id: int,
    qtype: str,
    prompt: str,
    answer,
    *,
    topic: str | None = None,
    choices: list[str] | None = None,
    rubric=None,
    skeleton: str | None = None,
    language: str | None = None,
    explanation: str | None = None,
) -> int:
    if qtype not in QUESTION_TYPES:
        raise ValueError(f"unknown type: {qtype}")
    # Normalize: rubric can come in as a list of bullets; flatten to text.
    if isinstance(rubric, list):
        rubric = "\n".join(f"- {b}" for b in rubric)
    # answer for `multi` may come as a list — store canonically as JSON.
    if isinstance(answer, list):
        answer = json.dumps(answer)
    ts = now()
    with cursor() as c:
        cur = c.execute(
            """
            INSERT INTO questions
                (user_id, deck_id, type, topic, prompt, choices, answer, rubric, created_at, skeleton, language, explanation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                deck_id,
                qtype,
                topic,
                prompt,
                json.dumps(choices) if choices else None,
                answer,
                rubric,
                ts,
                skeleton if (skeleton and qtype == "code") else None,
                language if qtype == "code" else None,
                explanation,
            ),
        )
        qid = cur.lastrowid
        c.execute(
            "INSERT INTO cards (question_id, step, next_due) VALUES (?, 0, ?)",
            (qid, ts),
        )
        return qid


def update_question(
    user_id: str,
    qid: int,
    *,
    qtype: str,
    prompt: str,
    answer,
    topic: str | None = None,
    choices: list[str] | None = None,
    rubric=None,
    skeleton: str | None = None,
    language: str | None = None,
) -> None:
    """In-place edit of an existing question. Same field shape as
    add_question. Does NOT touch the cards/reviews tables — SRS state
    is preserved across edits. Raises ValueError if no row matches
    (user_id, qid)."""
    if qtype not in QUESTION_TYPES:
        raise ValueError(f"unknown type: {qtype}")
    if isinstance(rubric, list):
        rubric = "\n".join(f"- {b}" for b in rubric)
    if isinstance(answer, list):
        answer = json.dumps(answer)
    with cursor() as c:
        cur = c.execute(
            """UPDATE questions
                  SET type = ?,
                      topic = ?,
                      prompt = ?,
                      choices = ?,
                      answer = ?,
                      rubric = ?,
                      skeleton = ?,
                      language = ?
                WHERE id = ? AND user_id = ?""",
            (
                qtype,
                topic,
                prompt,
                json.dumps(choices) if choices else None,
                answer,
                rubric,
                skeleton if (skeleton and qtype == "code") else None,
                language if qtype == "code" else None,
                qid,
                user_id,
            ),
        )
        if cur.rowcount == 0:
            raise ValueError(f"question {qid} not found for user")


def list_questions(user_id: str, deck_id: int) -> list[dict]:
    with cursor() as c:
        rows = c.execute(
            """
            SELECT q.id, q.type, q.topic, q.prompt, q.suspended,
                   q.answer, q.choices, q.rubric, q.skeleton, q.language,
                   cards.step, cards.next_due, cards.last_review,
                   (SELECT COUNT(*) FROM reviews r WHERE r.question_id=q.id) AS attempts,
                   (SELECT COUNT(*) FROM reviews r
                     WHERE r.question_id=q.id AND r.result='right') AS rights
              FROM questions q
              LEFT JOIN cards ON cards.question_id = q.id
             WHERE q.deck_id = ? AND q.user_id = ?
             ORDER BY cards.next_due ASC, q.id ASC
            """,
            (deck_id, user_id),
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            # Decode choices JSON for the deck-page preview dialog. Stored
            # as JSON text in DB; surfacing as a Python list keeps the
            # template free of inline json.loads calls.
            if d.get("choices"):
                try:
                    d["choices_list"] = json.loads(d["choices"])
                    if not isinstance(d["choices_list"], list):
                        d["choices_list"] = []
                except (ValueError, TypeError):
                    d["choices_list"] = []
            else:
                d["choices_list"] = []
            out.append(d)
        return out


def question_prompts_for_deck(user_id: str, deck_id: int) -> list[str]:
    """Used by the generator to avoid duplicates within a user's own deck."""
    with cursor() as c:
        rows = c.execute(
            "SELECT prompt FROM questions WHERE deck_id = ? AND user_id = ?",
            (deck_id, user_id),
        ).fetchall()
        return [r["prompt"] for r in rows]


def get_question(user_id: str, qid: int) -> dict | None:
    """Fetch a question, scoped to the user's own questions only. Returns
    None if qid doesn't exist OR belongs to another user — same response
    so we don't leak existence across users."""
    with cursor() as c:
        row = c.execute(
            """
            SELECT q.*, cards.step, cards.next_due
              FROM questions q LEFT JOIN cards ON cards.question_id = q.id
             WHERE q.id = ? AND q.user_id = ?
            """,
            (qid, user_id),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("choices"):
            d["choices_list"] = json.loads(d["choices"])
        else:
            d["choices_list"] = []
        return d


def due_questions(user_id: str, deck_id: int, limit: int = 3) -> list[dict]:
    """Cards due now, oldest-due first. Falls back to never-attempted-yet."""
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
        return [get_question(user_id, r["id"]) for r in rows]


def record_review(user_id: str, qid: int, result: str, user_answer: str, notes: str = "") -> dict:
    """Record a review and advance/reset the SRS step.

    Verifies the question belongs to the user before mutating SRS state —
    defense in depth in case a route misses the check.

    Returns the new card state.
    """
    try:
        verdict = Verdict(result)
    except ValueError as e:
        raise ValueError(f"unknown result: {result}") from e
    ts = datetime.now(timezone.utc)
    with cursor() as c:
        # Verify ownership.
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
            "UPDATE cards SET step = ?, next_due = ?, last_review = ? " "WHERE question_id = ?",
            (new_step, next_due, ts.isoformat(), qid),
        )
        return {"step": new_step, "next_due": next_due, "interval_minutes": interval}


def set_suspended(user_id: str, qid: int, suspended: bool) -> None:
    with cursor() as c:
        c.execute(
            "UPDATE questions SET suspended = ? WHERE id = ? AND user_id = ?",
            (1 if suspended else 0, qid, user_id),
        )


# =============================================================================
# Study sessions
# =============================================================================
#
# A "session" is the cross-device state that lets a user start studying on one
# device, walk away, and pick up on another. Each session belongs to a single
# deck and tracks the current card, the in-progress draft (typed-but-not-
# submitted), the set of cards already answered in this session, and a version
# integer that bumps on every server-side mutation. POSTs from the client must
# include the version; if it's stale we raise StaleVersionError so the client
# can show a "this session moved on another device — refresh" banner.

import uuid as _uuid


# Sentinel raised when a version-checked session mutation fails because the
# session has been advanced on another device. The route handler turns this
# into a 409 Conflict the client interprets as "show stale banner."
class StaleVersionError(Exception):
    def __init__(self, current_version: int):
        super().__init__(f"stale session version (current is {current_version})")
        self.current_version = current_version


def _new_session_id() -> str:
    return _uuid.uuid4().hex[:16]


def device_label_from_ua(ua: str | None) -> str:
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


def create_session(user_id: str, deck_id: int, device_label: str) -> str:
    """Create a fresh session for a deck. Returns the session id. Picks the
    first due card and seeds current_draft from its skeleton (if any)."""
    ts = now()
    sid = _new_session_id()
    next_q = _pick_next_question_for_session(user_id, deck_id, sid)
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


def get_session(user_id: str, sid: str) -> dict | None:
    """Fetch a session, scoped to the user. Returns None if the session
    doesn't exist OR belongs to another user."""
    with cursor() as c:
        row = c.execute(
            "SELECT * FROM study_sessions WHERE id = ? AND user_id = ?",
            (sid, user_id),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        for k in ("last_answered_verdict", "last_answered_state"):
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except json.JSONDecodeError:
                    pass
        return d


def _pick_next_question_for_session(user_id: str, deck_id: int, sid: str) -> dict | None:
    """Return the next question this session should show: a card that's due
    AND hasn't been answered in this session yet. Returns None if no such
    card (session is complete)."""
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
        return get_question(user_id, row["id"]) if row else None


def find_active_session_for_deck(user_id: str, deck_id: int) -> dict | None:
    """Used by /study/{deck}/begin to auto-resume."""
    with cursor() as c:
        row = c.execute(
            "SELECT * FROM study_sessions "
            " WHERE deck_id = ? AND user_id = ? AND status = 'active' "
            " ORDER BY last_active DESC LIMIT 1",
            (deck_id, user_id),
        ).fetchone()
        return dict(row) if row else None


def list_recent_sessions(user_id: str, limit: int = 5) -> list[dict]:
    """Recent active sessions for this user across all their decks.

    Side-effect: ages out THIS USER's sessions idle for >7d into
    status='abandoned'. Cheap to do on each list call rather than wiring
    a separate reaper.
    """
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
        return [dict(r) for r in rows]


# All mutation functions below take user_id and add it to the WHERE clause —
# defense in depth so a forgetful route can't accidentally let one user
# mutate another's session.


def update_session_draft(user_id: str, sid: str, draft: str, expected_version: int) -> int:
    """Save the in-progress draft. Version-checked. Returns the new version."""
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


def record_session_answer_sync(
    user_id: str,
    sid: str,
    question_id: int,
    expected_version: int,
    user_answer: str,
    verdict: dict,
    state: dict,
) -> int:
    """Synchronous answer recording (mcq/multi). Records the answer, sets
    state='showing-result', stores cached verdict/state. Bumps version.
    Returns new version."""
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


def set_session_grading(
    user_id: str, sid: str, question_id: int, workflow_id: str, expected_version: int
) -> int:
    """Used when a code/short submission kicks off a grading workflow.
    Sets state='grading', stores the workflow id, version-checked."""
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


def session_grading_completed(
    user_id: str, sid: str, question_id: int, verdict: dict, state: dict, workflow_id: str
) -> None:
    """Called from a polling endpoint once the grading workflow finishes.
    Stamps the answer + transitions to showing-result. Not version-checked
    because this is server-side reconciliation, not user input. Scoped to
    user_id so cross-user reconciliation can't happen."""
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


def advance_session(user_id: str, sid: str, expected_version: int) -> int:
    """Move from showing-result to the next due card (or completed)."""
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
        next_q = _pick_next_question_for_session(user_id, row["deck_id"], sid)
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


def abandon_session(user_id: str, sid: str) -> None:
    with cursor() as c:
        c.execute(
            "UPDATE study_sessions "
            "   SET status = 'abandoned', last_active = ?, version = version + 1 "
            " WHERE id = ? AND user_id = ?",
            (now(), sid, user_id),
        )


if __name__ == "__main__":
    init()
    print(f"Initialized {DB_PATH}")
