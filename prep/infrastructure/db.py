"""SQLite connection + schema layer.

Owns: DB_PATH, the connection factory, the per-request cursor()
context manager, init() (schema bootstrap + idempotent migrations),
and the now() timestamp helper used by both init's seed-row inserts
and many domain operations downstream.

Bounded-context repositories (decks/repo.py, study/repo.py, etc.)
import `cursor` from here. They do NOT reach into a global connection
or hold their own — the cursor() context manager is the single
acquire/release point, scoped to one logical transaction per call.

This module has no inbound deps on bounded contexts — it sits at the
bottom of the import graph.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# PREP_DB_PATH lets a deploy point at a per-environment data.sqlite
# living outside the immutable image (e.g. mounted volume at /data).
# Falls back to data.sqlite at the repo root for the dev case where
# source dir == data dir (the package lives at <repo>/prep/, so we
# go up one level).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = Path(os.environ.get("PREP_DB_PATH") or (_REPO_ROOT / "data.sqlite"))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def cursor():
    """Per-call sqlite connection. Auto-commits on clean exit, always
    closes. Caller iterates via `with cursor() as c: c.execute(...)`."""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now() -> str:
    """ISO-8601 UTC timestamp used everywhere we write a TEXT column.

    Lives here (rather than in domain) because almost every write goes
    through it, and it's a thin wrapper over the stdlib clock —
    fundamentally an infrastructure concern (do not inject 'wall clock'
    as a domain dependency unless you have a real reason)."""
    return datetime.now(timezone.utc).isoformat()


def init() -> None:
    """Schema bootstrap + idempotent migrations.

    Safe to call on every app boot. CREATE TABLE IF NOT EXISTS for the
    initial shape; ALTER TABLE / table-rebuild blocks for each
    historical migration step, each guarded by a `PRAGMA table_info`
    check so re-running is a no-op.
    """
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
            c.execute(
                """
                INSERT OR IGNORE INTO users
                  (tailscale_login, display_name, created_at, last_seen_at)
                VALUES (?, ?, ?, ?)
            """,
                (default_user, default_user.split("@")[0], now(), now()),
            )

            # Add user_id columns and backfill. SQLite ALTER doesn't allow
            # NOT NULL with non-constant default, so we add nullable, backfill
            # in one statement, then rely on app-level enforcement.
            for tbl in user_owned:
                cols = {r["name"] for r in c.execute(f"PRAGMA table_info({tbl})").fetchall()}
                if "user_id" not in cols:
                    c.execute(f"ALTER TABLE {tbl} ADD COLUMN user_id TEXT")
                    c.execute(
                        f"UPDATE {tbl} SET user_id = ? WHERE user_id IS NULL", (default_user,)
                    )

        # 3. The decks table originally had `name TEXT UNIQUE NOT NULL`. Now we
        #    want `UNIQUE(user_id, name)` so different users can have decks
        #    with the same name. Rebuild if the compound UNIQUE doesn't exist.
        has_compound_unique = False
        for idx in c.execute("PRAGMA index_list(decks)").fetchall():
            if idx["unique"]:
                cols = {
                    r["name"] for r in c.execute(f"PRAGMA index_info({idx['name']})").fetchall()
                }
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
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_questions_user_deck ON questions(user_id, deck_id)"
        )
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

        # 8. Trivia decks: a notification-driven deck mode. `deck_type`
        #    distinguishes the existing SRS flow ('srs', the default) from
        #    the new trivia flow ('trivia'). Trivia decks fire a periodic
        #    web push at `notification_interval_minutes` carrying the next
        #    queued question; tapping the push opens the card. No SRS
        #    `cards` row exists for trivia questions — their queue/answer
        #    state lives in `trivia_queue` keyed by question_id, with the
        #    rotation rule "answered → back of queue, regardless of
        #    correct/wrong".
        cols = {r["name"] for r in c.execute("PRAGMA table_info(decks)").fetchall()}
        if "deck_type" not in cols:
            c.execute("ALTER TABLE decks ADD COLUMN deck_type TEXT NOT NULL DEFAULT 'srs'")
        if "notification_interval_minutes" not in cols:
            c.execute("ALTER TABLE decks ADD COLUMN notification_interval_minutes INTEGER")
        if "last_notified_at" not in cols:
            # Tracks when the scheduler last fired a push for this trivia
            # deck. NULL = never fired (next scheduler tick will pick it
            # up immediately as long as the interval has passed since
            # deck creation).
            c.execute("ALTER TABLE decks ADD COLUMN last_notified_at TEXT")
        if "notifications_enabled" not in cols:
            # Per-deck on/off for the trivia notification cycle. Default
            # ON (1) so existing trivia decks keep firing through the
            # migration; users toggle OFF when they want a deck to go
            # quiet without deleting it.
            c.execute(
                "ALTER TABLE decks ADD COLUMN notifications_enabled INTEGER NOT NULL DEFAULT 1"
            )
        if "notification_ignored_streak" not in cols:
            # Exponential backoff for unattended trivia decks. The
            # scheduler bumps this every time a push fires without
            # the user answering anything in the deck, then waits
            # `base × 2 ** streak` (capped at MAX_DOUBLINGS) before
            # the next fire. Resets to 0 on any answer in the deck.
            # Default 0 = the deck fires at its base interval.
            c.execute(
                "ALTER TABLE decks ADD COLUMN notification_ignored_streak "
                "INTEGER NOT NULL DEFAULT 0"
            )

        # 9. Trivia card explanations: a short paragraph claude generates
        #    alongside the Q+A. Surfaced in the trivia card view as a
        #    "Deep dive" disclosure so the user can learn the why behind
        #    the answer, not just memorize it. NULL for older trivia
        #    cards generated before this column existed; UI hides the
        #    section when null.
        qcols = {r["name"] for r in c.execute("PRAGMA table_info(questions)").fetchall()}
        if "explanation" not in qcols:
            c.execute("ALTER TABLE questions ADD COLUMN explanation TEXT")

        # 10. Notification log: persist every push we send so the user
        #     can find missed/dismissed pushes after the fact.
        #     `seen_at` is set when the user opens /notify/log so the
        #     index can show an unread badge.
        c.executescript("""
            CREATE TABLE IF NOT EXISTS notifications_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   TEXT    NOT NULL,
                sent_at   TEXT    NOT NULL,
                title     TEXT    NOT NULL,
                body      TEXT    NOT NULL,
                url       TEXT    NOT NULL,
                source    TEXT    NOT NULL,
                seen_at   TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_notifications_log_user_sent
                ON notifications_log(user_id, sent_at DESC);
        """)
        c.executescript("""
            CREATE TABLE IF NOT EXISTS trivia_queue (
                question_id              INTEGER PRIMARY KEY REFERENCES questions(id) ON DELETE CASCADE,
                queue_position           INTEGER NOT NULL,
                last_answered_at         TEXT,
                last_answered_correctly  INTEGER  -- 0/1, NULL = never answered
            );
            CREATE INDEX IF NOT EXISTS idx_trivia_queue_pos ON trivia_queue(queue_position);
        """)
