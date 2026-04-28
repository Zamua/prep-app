"""SQLite layer for the interview-prep app.

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
import os
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
            CREATE TABLE IF NOT EXISTS users (
                tailscale_login  TEXT PRIMARY KEY,    -- email-shaped Tailscale login
                display_name     TEXT,
                profile_pic_url  TEXT,
                created_at       TEXT NOT NULL,
                last_seen_at     TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS decks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     TEXT NOT NULL REFERENCES users(tailscale_login) ON DELETE CASCADE,
                name        TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                UNIQUE (user_id, name)
            );

            CREATE TABLE IF NOT EXISTS questions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     TEXT NOT NULL REFERENCES users(tailscale_login) ON DELETE CASCADE,
                deck_id     INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
                type        TEXT NOT NULL,
                topic       TEXT,
                prompt      TEXT NOT NULL,
                choices     TEXT,
                answer      TEXT NOT NULL,
                rubric      TEXT,
                created_at  TEXT NOT NULL,
                suspended   INTEGER NOT NULL DEFAULT 0,
                skeleton    TEXT,  -- optional starter code for `code` questions
                language    TEXT
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
                user_id                     TEXT NOT NULL REFERENCES users(tailscale_login) ON DELETE CASCADE,
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
        # ---- Schema migrations (idempotent; runs on every boot) -------------

        # Older DBs may be missing skeleton / language columns on questions.
        cols = {r["name"] for r in c.execute("PRAGMA table_info(questions)").fetchall()}
        if "skeleton" not in cols:
            c.execute("ALTER TABLE questions ADD COLUMN skeleton TEXT")
        if "language" not in cols:
            c.execute("ALTER TABLE questions ADD COLUMN language TEXT")

        # Multi-user migration: thread `user_id` through user-owned tables.
        # Only fires when a pre-multi-user DB is detected (no user_id column
        # on any of the user-owned tables). Modern DBs short-circuit and
        # the inheriting-user upsert never runs — important so a deleted
        # "owner@local" doesn't get auto-resurrected on every boot.
        user_owned = ("decks", "questions", "study_sessions")
        needs_legacy_migration = False
        for tbl in user_owned:
            cols = {r["name"] for r in c.execute(f"PRAGMA table_info({tbl})").fetchall()}
            if "user_id" not in cols:
                needs_legacy_migration = True
                break

        if needs_legacy_migration:
            # Pre-multi-user DBs assumed a single inviting user.
            # PREP_DEFAULT_USER overrides the default; fall back to a literal
            # "owner@local" only because there's no real identity to attribute
            # the legacy data to.
            default_user = os.environ.get("PREP_DEFAULT_USER", "owner@local")
            c.execute("""
                INSERT OR IGNORE INTO users
                  (tailscale_login, display_name, created_at, last_seen_at)
                VALUES (?, ?, ?, ?)
            """, (default_user, default_user.split("@")[0], now(), now()))

            # Add user_id columns and backfill. SQLite ALTER doesn't allow
            # NOT NULL with non-constant default, so we add nullable, backfill
            # in one statement, then rely on app-level enforcement.
            for tbl in user_owned:
                cols = {r["name"] for r in c.execute(f"PRAGMA table_info({tbl})").fetchall()}
                if "user_id" not in cols:
                    c.execute(f"ALTER TABLE {tbl} ADD COLUMN user_id TEXT")
                    c.execute(f"UPDATE {tbl} SET user_id = ? WHERE user_id IS NULL", (default_user,))

        # 3. The decks table originally had `name TEXT UNIQUE NOT NULL`. Now we
        #    want `UNIQUE(user_id, name)` so different users can have decks
        #    with the same name. Rebuild if the compound UNIQUE doesn't exist.
        has_compound_unique = False
        for idx in c.execute("PRAGMA index_list(decks)").fetchall():
            if idx["unique"]:
                cols = {r["name"] for r in c.execute(f"PRAGMA index_info({idx['name']})").fetchall()}
                if cols == {"user_id", "name"}:
                    has_compound_unique = True
                    break
        if not has_compound_unique:
            # CRITICAL: disable FK enforcement for the rebuild. `DROP TABLE
            # decks` would otherwise CASCADE through every questions row
            # (FK: questions.deck_id → decks.id ON DELETE CASCADE), which
            # in turn cascades through cards and reviews. v0.3.0 shipped
            # without this guard and silently wiped a real user's deck on
            # the first prod migration — never again. SQLite's blessed
            # pattern for table rebuilds:
            # https://sqlite.org/lang_altertable.html#otheralter
            #
            # The PRAGMA must be set OUTSIDE any open transaction to take
            # effect. Commit any pending work on this connection first.
            c.commit()
            c.execute("PRAGMA foreign_keys = OFF")
            try:
                c.executescript("""
                    BEGIN;
                    CREATE TABLE decks_new (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id     TEXT NOT NULL,
                        name        TEXT NOT NULL,
                        created_at  TEXT NOT NULL,
                        UNIQUE (user_id, name)
                    );
                    INSERT INTO decks_new (id, user_id, name, created_at)
                      SELECT id, user_id, name, created_at FROM decks;
                    DROP TABLE decks;
                    ALTER TABLE decks_new RENAME TO decks;
                    COMMIT;
                """)
                # Verify no foreign-key invariants were violated. We preserve
                # `id` in the rebuild so questions.deck_id still resolves,
                # but defense-in-depth costs nothing.
                orphans = c.execute("PRAGMA foreign_key_check").fetchall()
                if orphans:
                    raise RuntimeError(
                        f"foreign_key_check failed after decks rebuild: "
                        f"{[dict(r) for r in orphans]}"
                    )
            finally:
                c.execute("PRAGMA foreign_keys = ON")

        # 4. user_id-dependent indexes — created last, after every table has
        #    the column. CREATE IF NOT EXISTS so re-running is a no-op.
        c.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON study_sessions(user_id, status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_questions_user_deck ON questions(user_id, deck_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_decks_user ON decks(user_id)")

        # 5. Notifications: per-user prefs (JSON blob) + push subscription table.
        cols = {r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()}
        if "notification_prefs" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN notification_prefs TEXT")  # JSON
        c.executescript("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                endpoint     TEXT PRIMARY KEY,
                user_id      TEXT NOT NULL REFERENCES users(tailscale_login) ON DELETE CASCADE,
                p256dh       TEXT NOT NULL,
                auth         TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_push_subs_user ON push_subscriptions(user_id);
        """)

        # 6. UI-created decks need a free-form context prompt — what claude
        #    sees when generating cards. Fills the role that DECK_CONTEXT[*]
        #    used to play in source code. Existing rows have NULL until
        #    re-described.
        cols = {r["name"] for r in c.execute("PRAGMA table_info(decks)").fetchall()}
        if "context_prompt" not in cols:
            c.execute("ALTER TABLE decks ADD COLUMN context_prompt TEXT")

        # 7. Editor input mode (vanilla | vim | emacs). Per-user profile
        #    setting that determines which CodeMirror keybinding extension
        #    loads when the user studies a code question.
        cols = {r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()}
        if "editor_input_mode" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN editor_input_mode TEXT")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# =============================================================================
# Users
# =============================================================================

def upsert_user(tailscale_login: str, display_name: str | None = None,
                profile_pic_url: str | None = None) -> dict:
    """Called on every authenticated request. Upserts the user row and bumps
    last_seen_at. Returns the user dict."""
    ts = now()
    with cursor() as c:
        c.execute(
            """INSERT INTO users (tailscale_login, display_name, profile_pic_url, created_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(tailscale_login) DO UPDATE SET
                 display_name = COALESCE(?, users.display_name),
                 profile_pic_url = COALESCE(?, users.profile_pic_url),
                 last_seen_at = ?""",
            (tailscale_login, display_name, profile_pic_url, ts, ts,
             display_name, profile_pic_url, ts),
        )
        return dict(c.execute(
            "SELECT * FROM users WHERE tailscale_login = ?", (tailscale_login,)
        ).fetchone())


# ---- Editor input mode (single-key user setting) --------------------------

EDITOR_INPUT_MODES = ("vanilla", "vim", "emacs")
DEFAULT_EDITOR_INPUT_MODE = "vanilla"


def get_editor_input_mode(user_id: str) -> str:
    """Returns the user's preferred CodeMirror input mode. Falls back to
    DEFAULT_EDITOR_INPUT_MODE if the column is NULL or unrecognised."""
    with cursor() as c:
        row = c.execute(
            "SELECT editor_input_mode FROM users WHERE tailscale_login = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return DEFAULT_EDITOR_INPUT_MODE
    val = row["editor_input_mode"]
    if val in EDITOR_INPUT_MODES:
        return val
    return DEFAULT_EDITOR_INPUT_MODE


def set_editor_input_mode(user_id: str, mode: str) -> None:
    if mode not in EDITOR_INPUT_MODES:
        raise ValueError(f"unknown editor input mode {mode!r}")
    with cursor() as c:
        c.execute(
            "UPDATE users SET editor_input_mode = ? WHERE tailscale_login = ?",
            (mode, user_id),
        )


# ---- Notification prefs (JSON blob on users) ------------------------------

import json as _json

# Default prefs for a fresh user — explicit opt-in, so mode starts off.
DEFAULT_NOTIFICATION_PREFS = {
    "mode": "off",                      # off | digest | when-ready
    "digest_hour": 9,                   # 0..23 local-tz hour for digest mode
    "tz": "America/New_York",           # IANA timezone name
    "threshold": 3,                     # min due cards for when-ready mode
    "quiet_hours_enabled": False,       # opt-in; when false, no quiet window
    "quiet_start_hour": 22,             # 0..23, only honored when enabled
    "quiet_end_hour": 8,
    # State (not user-edited; updated by the scheduler):
    "last_digest_date": None,           # ISO date "YYYY-MM-DD" in user tz
    "last_when_ready_at": None,         # ISO datetime UTC, debounce window
}


def get_notification_prefs(user_id: str) -> dict:
    """Return current prefs merged over defaults so callers always see every
    key. Defaults apply for users who've never opened settings."""
    with cursor() as c:
        row = c.execute(
            "SELECT notification_prefs FROM users WHERE tailscale_login = ?",
            (user_id,),
        ).fetchone()
    raw = row["notification_prefs"] if row and row["notification_prefs"] else None
    saved = _json.loads(raw) if raw else {}
    return {**DEFAULT_NOTIFICATION_PREFS, **saved}


def set_notification_prefs(user_id: str, prefs: dict) -> None:
    """Persist prefs. Caller is responsible for validation (we trust the
    settings route to clamp ranges and validate the mode enum)."""
    with cursor() as c:
        c.execute(
            "UPDATE users SET notification_prefs = ? WHERE tailscale_login = ?",
            (_json.dumps(prefs), user_id),
        )


# ---- Push subscriptions (DB-backed, one row per device) -------------------

def upsert_push_subscription(user_id: str, endpoint: str, p256dh: str, auth: str) -> None:
    ts = now()
    with cursor() as c:
        c.execute(
            """INSERT INTO push_subscriptions (endpoint, user_id, p256dh, auth, created_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(endpoint) DO UPDATE SET
                 user_id = excluded.user_id,
                 p256dh = excluded.p256dh,
                 auth = excluded.auth,
                 last_seen_at = excluded.last_seen_at""",
            (endpoint, user_id, p256dh, auth, ts, ts),
        )


def list_push_subscriptions(user_id: str) -> list[dict]:
    with cursor() as c:
        rows = c.execute(
            "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_push_subscription(endpoint: str) -> None:
    """Used to prune subscriptions the push service has rejected (404/410).
    Endpoint is the natural unique key; same endpoint can only be one user's."""
    with cursor() as c:
        c.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))


def list_users_with_push_subs() -> list[str]:
    """Return tailscale_login values for every user with at least one
    push subscription. Used by the scheduler so we don't iterate users who
    can't be reached anyway."""
    with cursor() as c:
        rows = c.execute(
            "SELECT DISTINCT user_id FROM push_subscriptions"
        ).fetchall()
    return [r["user_id"] for r in rows]


def count_due_for_user(user_id: str) -> int:
    """Total cards due-now across all of this user's decks. The scheduler
    uses this to decide whether to send a digest / threshold ping."""
    with cursor() as c:
        row = c.execute(
            """SELECT COUNT(*) AS n
                 FROM cards
                 JOIN questions ON questions.id = cards.question_id
                WHERE questions.user_id = ?
                  AND COALESCE(questions.suspended, 0) = 0
                  AND cards.next_due <= ?""",
            (user_id, now()),
        ).fetchone()
    return int(row["n"]) if row else 0


def deck_due_breakdown(user_id: str) -> list[tuple[str, int]]:
    """[(deck_name, due_count), ...] for digest body composition."""
    with cursor() as c:
        rows = c.execute(
            """SELECT d.name, COUNT(c.question_id) AS n
                 FROM decks d
                 LEFT JOIN questions q ON q.deck_id = d.id AND q.user_id = d.user_id
                 LEFT JOIN cards c ON c.question_id = q.id
                                  AND c.next_due <= ?
                                  AND COALESCE(q.suspended, 0) = 0
                WHERE d.user_id = ?
                GROUP BY d.id
               HAVING n > 0
                ORDER BY n DESC""",
            (now(), user_id),
        ).fetchall()
    return [(r["name"], int(r["n"])) for r in rows]


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


def list_decks(user_id: str) -> list[dict]:
    with cursor() as c:
        rows = c.execute(
            """
            SELECT d.id, d.name,
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
                (user_id, deck_id, type, topic, prompt, choices, answer, rubric, created_at, skeleton, language)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        qid = cur.lastrowid
        c.execute(
            "INSERT INTO cards (question_id, step, next_due) VALUES (?, 0, ?)",
            (qid, ts),
        )
        return qid


def list_questions(user_id: str, deck_id: int) -> list[dict]:
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
             WHERE q.deck_id = ? AND q.user_id = ?
             ORDER BY cards.next_due ASC, q.id ASC
            """,
            (deck_id, user_id),
        ).fetchall()
        return [dict(r) for r in rows]


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
    if result not in {"right", "wrong"}:
        raise ValueError(f"unknown result: {result}")
    ts = datetime.now(timezone.utc)
    with cursor() as c:
        # Verify ownership.
        owner = c.execute(
            "SELECT user_id FROM questions WHERE id = ?", (qid,)
        ).fetchone()
        if not owner or owner["user_id"] != user_id:
            raise ValueError(f"question {qid} not owned by {user_id}")
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
            (sid, user_id, deck_id, ts, ts, next_q["id"] if next_q else None,
             initial_draft, device_label),
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
            (question_id, json.dumps(verdict), json.dumps(state),
             ts, new_v, sid, user_id),
        )
        return new_v


def set_session_grading(user_id: str, sid: str, question_id: int, workflow_id: str,
                        expected_version: int) -> int:
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


def session_grading_completed(user_id: str, sid: str, question_id: int, verdict: dict,
                               state: dict, workflow_id: str) -> None:
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
