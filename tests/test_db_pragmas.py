"""Connection-level guarantees the merge transactions depend on:
readers that do not block on a writer, and a writer that waits out a
peer's lock instead of raising at it."""

from __future__ import annotations

from prep.infrastructure import db


def test_init_puts_the_database_in_wal(initialized_db: str):
    with db.cursor() as c:
        mode = c.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_connections_carry_a_non_zero_busy_timeout(initialized_db: str):
    conn = db._connect()
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] > 0
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_anonymous_schema_landed(initialized_db: str):
    with db.cursor() as c:
        ucols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        mcols = {r["name"] for r in c.execute("PRAGMA table_info(account_merges)")}
        indexes = {
            r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    assert "is_anonymous" in ucols
    assert mcols == {
        "id",
        "anon_user_id",
        "target_user_id",
        "started_at",
        "completed_at",
        "status",
        "counts",
        "error",
    }
    assert "idx_users_anon_last_seen" in indexes
    assert "idx_account_merges_anon" in indexes
    assert "idx_account_merges_target" in indexes


def test_init_is_idempotent(initialized_db: str):
    db.init()
    db.init()
    with db.cursor() as c:
        assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert c.execute("SELECT COUNT(*) FROM account_merges").fetchone()[0] == 0
