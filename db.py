"""SQLite layer for the interview-prep app.

Schema:
  decks       : one row per deck name
  questions   : prompt + type + answer key + rubric, one per card
  reviews     : every grading event, used to compute next-due
  cards       : one-to-one with questions, holds current SRS state

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
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "data.sqlite"

INTERVAL_LADDER_MINUTES = [
    10,           # wrong -> see again very soon
    24 * 60,      # 1d
    3 * 24 * 60,  # 3d
    7 * 24 * 60,
    14 * 24 * 60,
    30 * 24 * 60,
]

QUESTION_TYPES = {"code", "mcq", "multi", "short"}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def cursor():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with cursor() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS decks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT UNIQUE NOT NULL,
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS questions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                deck_id     INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
                type        TEXT NOT NULL,
                topic       TEXT,
                prompt      TEXT NOT NULL,
                choices     TEXT,
                answer      TEXT NOT NULL,
                rubric      TEXT,
                created_at  TEXT NOT NULL,
                suspended   INTEGER NOT NULL DEFAULT 0,
                skeleton    TEXT  -- optional starter code for `code` questions
            );

            CREATE TABLE IF NOT EXISTS cards (
                question_id INTEGER PRIMARY KEY REFERENCES questions(id) ON DELETE CASCADE,
                step        INTEGER NOT NULL DEFAULT 0,
                next_due    TEXT NOT NULL,
                last_review TEXT
            );

            CREATE TABLE IF NOT EXISTS reviews (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                question_id  INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
                ts           TEXT NOT NULL,
                result       TEXT NOT NULL,        -- 'right' | 'wrong'
                user_answer  TEXT,
                grader_notes TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_questions_deck ON questions(deck_id);
            CREATE INDEX IF NOT EXISTS idx_cards_due ON cards(next_due);
            CREATE INDEX IF NOT EXISTS idx_reviews_q ON reviews(question_id);

            CREATE TABLE IF NOT EXISTS study_sessions (
                id                          TEXT PRIMARY KEY,
                deck_id                     INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
                created_at                  TEXT NOT NULL,
                last_active                 TEXT NOT NULL,
                status                      TEXT NOT NULL DEFAULT 'active',         -- active | completed | abandoned
                state                       TEXT NOT NULL DEFAULT 'awaiting-answer', -- awaiting-answer | grading | showing-result
                current_question_id         INTEGER REFERENCES questions(id),
                current_draft               TEXT,
                current_grading_workflow_id TEXT,
                last_answered_qid           INTEGER,
                last_answered_verdict       TEXT,  -- JSON
                last_answered_state         TEXT,  -- JSON (SRS state from grading)
                version                     INTEGER NOT NULL DEFAULT 1,
                device_label                TEXT
            );

            CREATE TABLE IF NOT EXISTS study_session_answers (
                session_id   TEXT NOT NULL REFERENCES study_sessions(id) ON DELETE CASCADE,
                question_id  INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
                answered_at  TEXT NOT NULL,
                result       TEXT NOT NULL,
                workflow_id  TEXT,
                PRIMARY KEY (session_id, question_id)
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_status ON study_sessions(status, last_active);
            CREATE INDEX IF NOT EXISTS idx_sessions_deck ON study_sessions(deck_id, status);
            """
        )
        # Migration: older DBs may have the questions table without
        # `skeleton` / `language`. Add if missing — idempotent on each boot.
        cols = [r["name"] for r in c.execute("PRAGMA table_info(questions)").fetchall()]
        if "skeleton" not in cols:
            c.execute("ALTER TABLE questions ADD COLUMN skeleton TEXT")
        if "language" not in cols:
            c.execute("ALTER TABLE questions ADD COLUMN language TEXT")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_or_create_deck(name: str) -> int:
    with cursor() as c:
        row = c.execute("SELECT id FROM decks WHERE name = ?", (name,)).fetchone()
        if row:
            return row["id"]
        cur = c.execute(
            "INSERT INTO decks (name, created_at) VALUES (?, ?)", (name, now())
        )
        return cur.lastrowid


def list_decks() -> list[dict]:
    with cursor() as c:
        rows = c.execute(
            """
            SELECT d.id, d.name,
                   COUNT(q.id) AS total,
                   SUM(CASE WHEN cards.next_due <= ? AND COALESCE(q.suspended,0)=0
                            THEN 1 ELSE 0 END) AS due
              FROM decks d
              LEFT JOIN questions q ON q.deck_id = d.id
              LEFT JOIN cards ON cards.question_id = q.id
             GROUP BY d.id
             ORDER BY d.name
            """,
            (now(),),
        ).fetchall()
        return [dict(r) for r in rows]


def add_question(
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
                (deck_id, type, topic, prompt, choices, answer, rubric, created_at, skeleton, language)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
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
            ),
        )
        qid = cur.lastrowid
        c.execute(
            "INSERT INTO cards (question_id, step, next_due) VALUES (?, 0, ?)",
            (qid, ts),
        )
        return qid


def list_questions(deck_id: int) -> list[dict]:
    with cursor() as c:
        rows = c.execute(
            """
            SELECT q.id, q.type, q.topic, q.prompt, q.suspended,
                   cards.step, cards.next_due, cards.last_review,
                   (SELECT COUNT(*) FROM reviews r WHERE r.question_id=q.id) AS attempts,
                   (SELECT COUNT(*) FROM reviews r
                     WHERE r.question_id=q.id AND r.result='right') AS rights
              FROM questions q
              LEFT JOIN cards ON cards.question_id = q.id
             WHERE q.deck_id = ?
             ORDER BY cards.next_due ASC, q.id ASC
            """,
            (deck_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def question_prompts_for_deck(deck_id: int) -> list[str]:
    """Used by the generator to avoid duplicates."""
    with cursor() as c:
        rows = c.execute(
            "SELECT prompt FROM questions WHERE deck_id = ?", (deck_id,)
        ).fetchall()
        return [r["prompt"] for r in rows]


def get_question(qid: int) -> dict | None:
    with cursor() as c:
        row = c.execute(
            """
            SELECT q.*, cards.step, cards.next_due
              FROM questions q LEFT JOIN cards ON cards.question_id = q.id
             WHERE q.id = ?
            """,
            (qid,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("choices"):
            d["choices_list"] = json.loads(d["choices"])
        else:
            d["choices_list"] = []
        return d


def due_questions(deck_id: int, limit: int = 3) -> list[dict]:
    """Cards due now, oldest-due first. Falls back to never-attempted-yet."""
    ts = now()
    with cursor() as c:
        rows = c.execute(
            """
            SELECT q.id
              FROM questions q
              JOIN cards ON cards.question_id = q.id
             WHERE q.deck_id = ?
               AND COALESCE(q.suspended, 0) = 0
               AND cards.next_due <= ?
             ORDER BY cards.next_due ASC
             LIMIT ?
            """,
            (deck_id, ts, limit),
        ).fetchall()
        return [get_question(r["id"]) for r in rows]


def record_review(qid: int, result: str, user_answer: str, notes: str = "") -> dict:
    """Record a review and advance/reset the SRS step.

    Returns the new card state.
    """
    if result not in {"right", "wrong"}:
        raise ValueError(f"unknown result: {result}")
    ts = datetime.now(timezone.utc)
    with cursor() as c:
        row = c.execute(
            "SELECT step FROM cards WHERE question_id = ?", (qid,)
        ).fetchone()
        if not row:
            raise ValueError(f"no card for question {qid}")
        step = row["step"]
        if result == "wrong":
            new_step = 0
            interval = INTERVAL_LADDER_MINUTES[0]
        else:
            new_step = min(step + 1, len(INTERVAL_LADDER_MINUTES) - 1)
            interval = INTERVAL_LADDER_MINUTES[new_step]
        next_due = (ts + timedelta(minutes=interval)).isoformat()
        c.execute(
            "INSERT INTO reviews (question_id, ts, result, user_answer, grader_notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (qid, ts.isoformat(), result, user_answer, notes),
        )
        c.execute(
            "UPDATE cards SET step = ?, next_due = ?, last_review = ? "
            "WHERE question_id = ?",
            (new_step, next_due, ts.isoformat(), qid),
        )
        return {"step": new_step, "next_due": next_due, "interval_minutes": interval}


def set_suspended(qid: int, suspended: bool) -> None:
    with cursor() as c:
        c.execute(
            "UPDATE questions SET suspended = ? WHERE id = ?",
            (1 if suspended else 0, qid),
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


def create_session(deck_id: int, device_label: str) -> str:
    """Create a fresh session for a deck. Returns the session id. Picks the
    first due card and seeds current_draft from its skeleton (if any)."""
    ts = now()
    sid = _new_session_id()
    next_q = _pick_next_question_for_session(deck_id, sid)
    initial_draft = (next_q.get("skeleton") or "") if next_q else ""
    with cursor() as c:
        c.execute(
            """
            INSERT INTO study_sessions
                (id, deck_id, created_at, last_active, status, state,
                 current_question_id, current_draft, version, device_label)
            VALUES (?, ?, ?, ?, 'active', 'awaiting-answer', ?, ?, 1, ?)
            """,
            (sid, deck_id, ts, ts, next_q["id"] if next_q else None,
             initial_draft, device_label),
        )
    return sid


def get_session(sid: str) -> dict | None:
    with cursor() as c:
        row = c.execute(
            "SELECT * FROM study_sessions WHERE id = ?", (sid,)
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


def _pick_next_question_for_session(deck_id: int, sid: str) -> dict | None:
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
             WHERE q.deck_id = ?
               AND COALESCE(q.suspended, 0) = 0
               AND cards.next_due <= ?
               AND q.id NOT IN (
                   SELECT question_id FROM study_session_answers WHERE session_id = ?
               )
             ORDER BY cards.next_due ASC
             LIMIT 1
            """,
            (deck_id, ts, sid),
        ).fetchone()
        return get_question(row["id"]) if row else None


def find_active_session_for_deck(deck_id: int) -> dict | None:
    """Used by /study/{deck}/begin to auto-resume."""
    with cursor() as c:
        row = c.execute(
            "SELECT * FROM study_sessions "
            " WHERE deck_id = ? AND status = 'active' "
            " ORDER BY last_active DESC LIMIT 1",
            (deck_id,),
        ).fetchone()
        return dict(row) if row else None


def list_recent_sessions(limit: int = 5) -> list[dict]:
    """Recent active sessions across all decks; for the index page block.

    Side-effect: ages out sessions idle for >7d into status='abandoned'.
    Cheap to do on each list call rather than wiring a separate reaper.
    """
    abandon_before = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with cursor() as c:
        c.execute(
            "UPDATE study_sessions SET status = 'abandoned' "
            " WHERE status = 'active' AND last_active < ?",
            (abandon_before,),
        )
        rows = c.execute(
            """
            SELECT s.*, d.name AS deck_name,
                   q.prompt AS current_prompt, q.type AS current_type
              FROM study_sessions s
              JOIN decks d ON d.id = s.deck_id
              LEFT JOIN questions q ON q.id = s.current_question_id
             WHERE s.status = 'active'
             ORDER BY s.last_active DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def update_session_draft(sid: str, draft: str, expected_version: int) -> int:
    """Save the in-progress draft. Version-checked. Returns the new version."""
    ts = now()
    with cursor() as c:
        row = c.execute(
            "SELECT version FROM study_sessions WHERE id = ?", (sid,)
        ).fetchone()
        if not row:
            raise ValueError(f"session {sid} not found")
        if row["version"] != expected_version:
            raise StaleVersionError(row["version"])
        new_v = expected_version + 1
        c.execute(
            "UPDATE study_sessions "
            "   SET current_draft = ?, last_active = ?, version = ? "
            " WHERE id = ?",
            (draft, ts, new_v, sid),
        )
        return new_v


def record_session_answer_sync(
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
            "SELECT version FROM study_sessions WHERE id = ?", (sid,)
        ).fetchone()
        if not row:
            raise ValueError(f"session {sid} not found")
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
              WHERE id = ?""",
            (question_id, json.dumps(verdict), json.dumps(state), ts, new_v, sid),
        )
        return new_v


def set_session_grading(sid: str, question_id: int, workflow_id: str,
                        expected_version: int) -> int:
    """Used when a code/short submission kicks off a grading workflow.
    Sets state='grading', stores the workflow id, version-checked."""
    ts = now()
    with cursor() as c:
        row = c.execute(
            "SELECT version FROM study_sessions WHERE id = ?", (sid,)
        ).fetchone()
        if not row:
            raise ValueError(f"session {sid} not found")
        if row["version"] != expected_version:
            raise StaleVersionError(row["version"])
        new_v = expected_version + 1
        c.execute(
            """UPDATE study_sessions SET
                state = 'grading',
                current_grading_workflow_id = ?,
                last_active = ?,
                version = ?
              WHERE id = ?""",
            (workflow_id, ts, new_v, sid),
        )
        return new_v


def session_grading_completed(sid: str, question_id: int, verdict: dict,
                               state: dict, workflow_id: str) -> None:
    """Called from a polling endpoint once the grading workflow finishes.
    Stamps the answer + transitions to showing-result. Not version-checked
    because this is server-side reconciliation, not user input."""
    ts = now()
    with cursor() as c:
        row = c.execute(
            "SELECT state, version FROM study_sessions WHERE id = ?", (sid,)
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
              WHERE id = ?""",
            (question_id, json.dumps(verdict), json.dumps(state), ts, sid),
        )


def advance_session(sid: str, expected_version: int) -> int:
    """Move from showing-result to the next due card (or completed)."""
    ts = now()
    with cursor() as c:
        row = c.execute(
            "SELECT version, deck_id FROM study_sessions WHERE id = ?", (sid,)
        ).fetchone()
        if not row:
            raise ValueError(f"session {sid} not found")
        if row["version"] != expected_version:
            raise StaleVersionError(row["version"])
        next_q = _pick_next_question_for_session(row["deck_id"], sid)
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
                  WHERE id = ?""",
                (ts, new_v, sid),
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
                  WHERE id = ?""",
                (next_q["id"], next_q.get("skeleton") or "", ts, new_v, sid),
            )
        return new_v


def abandon_session(sid: str) -> None:
    with cursor() as c:
        c.execute(
            "UPDATE study_sessions "
            "   SET status = 'abandoned', last_active = ?, version = version + 1 "
            " WHERE id = ?",
            (now(), sid),
        )


if __name__ == "__main__":
    init()
    print(f"Initialized {DB_PATH}")
