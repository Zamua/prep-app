"""The synthetic snapshot is prod-shaped and reproducible.

The exporter, importer and verifier are all built against this fixture
until a real snapshot is authorised, so every shape the spec names has to
be in it (docs/PHASE-6.md F1) and a seed has to fix the file.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from prep.migrate import layout, synth
from prep.migrate.snapshot import has_sidecars, open_snapshot, sha256_file

from .conftest import ANONYMOUS, HEAVY_QUESTIONS, HEAVY_REVIEWS, NOW, SEED, USERS


def _counts(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def test_the_output_is_a_snapshot_not_a_live_database(snapshot: Path):
    assert not has_sidecars(snapshot)
    conn = open_snapshot(snapshot)
    conn.close()


def test_the_schema_is_the_apps_own(snapshot: Path):
    conn = open_snapshot(snapshot)
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        conn.close()
    assert set(layout.PYTHON_TABLES) <= tables


def test_the_same_seed_and_clock_produce_the_same_file(tmp_path: Path, snapshot: Path):
    twin = tmp_path / "twin.sqlite"
    synth.generate(
        twin,
        users=USERS,
        seed=SEED,
        anonymous=ANONYMOUS,
        heavy_questions=HEAVY_QUESTIONS,
        heavy_reviews=HEAVY_REVIEWS,
        now=NOW,
    )
    assert sha256_file(twin) == sha256_file(snapshot)


def test_a_different_seed_produces_a_different_file(tmp_path: Path, snapshot: Path):
    other = tmp_path / "other.sqlite"
    synth.generate(
        other,
        users=USERS,
        seed=SEED + 1,
        anonymous=ANONYMOUS,
        heavy_questions=HEAVY_QUESTIONS,
        heavy_reviews=HEAVY_REVIEWS,
        now=NOW,
    )
    assert sha256_file(other) != sha256_file(snapshot)


def test_every_shape_the_rehearsal_needs_is_present(snapshot: Path, plan: synth.Plan):
    conn = open_snapshot(snapshot)
    try:
        assert _counts(conn, "SELECT COUNT(*) FROM users") == USERS
        assert _counts(conn, "SELECT COUNT(*) FROM users WHERE is_anonymous = 1") == ANONYMOUS

        heavy_questions = _counts(
            conn, "SELECT COUNT(*) FROM questions WHERE user_id = ?", (plan.heavy,)
        )
        assert heavy_questions == HEAVY_QUESTIONS
        assert (
            _counts(
                conn,
                "SELECT COUNT(*) FROM reviews r JOIN questions q ON q.id = r.question_id"
                " WHERE q.user_id = ?",
                (plan.heavy,),
            )
            == HEAVY_REVIEWS
        )

        assert _counts(conn, "SELECT COUNT(*) FROM account_merges WHERE status = 'started'") == 1
        assert _counts(conn, "SELECT COUNT(*) FROM account_merges WHERE status = 'completed'") == 1
        assert (
            _counts(
                conn,
                "SELECT COUNT(*) FROM byok_credentials WHERE provider = 'claude-subscription'",
            )
            == 1
        )
        assert _counts(conn, "SELECT COUNT(*) FROM api_tokens") == 1
        assert (
            _counts(
                conn, "SELECT COUNT(*) FROM push_subscriptions WHERE user_id = ?", (plan.push_user,)
            )
            == 2
        )
        assert _counts(conn, "SELECT COUNT(*) FROM active_workflows") > 0
        assert _counts(conn, "SELECT COUNT(*) FROM study_sessions WHERE state = 'grading'") == 1
        assert _counts(conn, "SELECT COUNT(*) FROM trivia_queue") > 0
        assert _counts(conn, "SELECT COUNT(*) FROM notifications_log") > 0
        assert _counts(conn, "SELECT COUNT(*) FROM offline_sync_idempotency") > 0
        assert _counts(conn, "SELECT COUNT(*) FROM grading_idempotency") > 0

        states = {r[0] for r in conn.execute("SELECT DISTINCT fsrs_state FROM cards")}
        assert states == {1, 2, 3}
        assert _counts(conn, "SELECT COUNT(*) FROM cards WHERE stability IS NULL") > 0

        retentions = {
            r[0] for r in conn.execute("SELECT desired_retention FROM users") if r[0] is not None
        }
        assert {synth.RETENTION_MIN, synth.RETENTION_MAX} <= retentions

        # The anonymous account with nothing at all: a directory entry and a
        # profile with no rows behind them.
        assert (
            _counts(conn, "SELECT COUNT(*) FROM decks WHERE user_id = ?", (plan.empty_anonymous,))
            == 0
        )
    finally:
        conn.close()


def test_ids_stay_below_the_cell_id_block(snapshot: Path):
    """Every imported row keeps its Python id, and the cell seeds its
    sequences to `idx * 2^32` afterwards; an id at or above the block
    would collide with a later cell's."""
    id_block = 2**32
    conn = open_snapshot(snapshot)
    try:
        for table in ("decks", "questions", "reviews", "notifications_log", "api_tokens"):
            largest = conn.execute(f'SELECT MAX(id) FROM "{table}"').fetchone()[0]
            assert largest is None or largest < id_block, table
    finally:
        conn.close()


def test_the_generator_leaves_no_working_database_behind(tmp_path: Path):
    out = tmp_path / "nested" / "snap.sqlite"
    synth.generate(out, users=4, seed=1, anonymous=1, heavy_questions=3, heavy_reviews=5, now=NOW)
    assert sorted(p.name for p in out.parent.iterdir()) == ["snap.sqlite"]
