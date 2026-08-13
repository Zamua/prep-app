"""`merge_anonymous_into`: the policy map, the guards around it, and
what survives a failure (docs/ANONYMOUS-ACCOUNTS.md section 5)."""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager

import pytest

from prep.auth import merge as merge_mod
from prep.auth.merge import (
    CARRIED_USER_COLUMNS,
    DELETE,
    DROPPED_USER_COLUMNS,
    POLICY,
    POLICY_TABLES,
    LeftoverAnonRows,
    UnknownUserScopedTable,
    discover_user_scoped_tables,
    merge_anonymous_into,
    previous_user_ids,
)
from prep.infrastructure.db import cursor
from tests.anon_support import seed_anon_user

ANON = "anon:" + "ab" * 16
OTHER_ANON = "anon:" + "cd" * 16

# Every table whose ownership is derived from a parent row. The merge
# must move their parents and issue no statement of its own.
DERIVED_TABLES = ("cards", "reviews", "study_session_answers", "trivia_queue")


def seed_all_tables(user_id: str, tag: str) -> int:
    """One row per policy table for `user_id`, plus a row in each
    derived table. `tag` keeps the rows with their own unique keys
    apart across users. Returns the deck id."""
    with cursor() as c:
        deck = c.execute(
            "INSERT INTO decks (user_id, name, display_name, created_at)"
            " VALUES (?, ?, ?, '2026-01-01T00:00:00+00:00')",
            (user_id, f"{tag}-deck", f"{tag} deck"),
        )
        deck_id = deck.lastrowid
        question = c.execute(
            "INSERT INTO questions (user_id, deck_id, type, prompt, answer, created_at)"
            " VALUES (?, ?, 'short', ?, 'Paris', '2026-01-01T00:00:00+00:00')",
            (user_id, deck_id, f"{tag} capital of France?"),
        )
        qid = question.lastrowid
        c.execute(
            "INSERT INTO cards (question_id, step, next_due, stability, difficulty, fsrs_state)"
            " VALUES (?, 2, '2026-06-01T00:00:00+00:00', 7.5, 5.0, 2)",
            (qid,),
        )
        c.execute(
            "INSERT INTO reviews (question_id, ts, result, user_answer)"
            " VALUES (?, '2026-01-02T00:00:00+00:00', 'right', 'Paris')",
            (qid,),
        )
        c.execute(
            "INSERT INTO study_sessions (id, user_id, deck_id, created_at, last_active)"
            " VALUES (?, ?, ?, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (f"{tag}-session", user_id, deck_id),
        )
        c.execute(
            "INSERT INTO study_session_answers (session_id, question_id, answered_at, result)"
            " VALUES (?, ?, '2026-01-02T00:00:00+00:00', 'right')",
            (f"{tag}-session", qid),
        )
        c.execute(
            "INSERT INTO trivia_sessions (id, user_id, deck_id, started_at, last_active)"
            " VALUES (?, ?, ?, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (f"{tag}-trivia", user_id, deck_id),
        )
        c.execute(
            "INSERT INTO trivia_queue (question_id, queue_position) VALUES (?, 1)",
            (qid,),
        )
        c.execute(
            "INSERT INTO notifications_log (user_id, sent_at, title, body, url, source)"
            " VALUES (?, '2026-01-01T00:00:00+00:00', 'due', 'cards', '/', 'digest')",
            (user_id,),
        )
        c.execute(
            "INSERT INTO active_workflows"
            " (workflow_id, user_login, workflow_type, status, started_at, url_path)"
            " VALUES (?, ?, 'plan', 'running', '2026-01-01T00:00:00+00:00', '/plan/x')",
            (f"{tag}-wf", user_id),
        )
        c.execute(
            "INSERT INTO offline_sync_idempotency"
            " (user_id, client_id, kind, status, created_at)"
            " VALUES (?, ?, 'review', 'applied', '2026-01-01T00:00:00+00:00')",
            (user_id, f"{tag}-client"),
        )
        c.execute(
            "INSERT INTO instant_generations (ip, created_at, outcome, user_id)"
            " VALUES ('10.0.0.1', '2026-01-01T00:00:00+00:00', 'ok', ?)",
            (user_id,),
        )
        c.execute(
            "INSERT INTO push_subscriptions"
            " (endpoint, user_id, p256dh, auth, created_at, last_seen_at)"
            " VALUES (?, ?, 'p', 'a', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (f"https://push.example/{tag}", user_id),
        )
        c.execute(
            "INSERT INTO byok_credentials"
            " (user_id, provider, ciphertext, key_prefix, created_at)"
            " VALUES (?, 'anthropic', 'ct', 'sk-…', '2026-01-01T00:00:00+00:00')",
            (user_id,),
        )
        c.execute(
            "INSERT INTO api_tokens (user_id, token_hash, key_prefix, created_at)"
            " VALUES (?, ?, 'prep_pat_…', '2026-01-01T00:00:00+00:00')",
            (user_id, f"{tag}-hash"),
        )
    return deck_id


def count_for(table: str, column: str, user_id: str) -> int:
    with cursor() as c:
        return c.execute(
            f'SELECT COUNT(*) AS n FROM "{table}" WHERE "{column}" = ?', (user_id,)
        ).fetchone()["n"]


def audit_rows() -> list[dict]:
    with cursor() as c:
        return [dict(r) for r in c.execute("SELECT * FROM account_merges ORDER BY id").fetchall()]


def user_exists(user_id: str) -> bool:
    with cursor() as c:
        return (
            c.execute(
                "SELECT COUNT(*) AS n FROM users WHERE tailscale_login = ?", (user_id,)
            ).fetchone()["n"]
            == 1
        )


@pytest.fixture
def sql_trace(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every statement the merge executes, in order."""
    statements: list[str] = []
    real = merge_mod.cursor

    @contextmanager
    def traced():
        with real() as conn:
            conn.set_trace_callback(statements.append)
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)

    monkeypatch.setattr(merge_mod, "cursor", traced)
    return statements


# ---- schema drift -----------------------------------------------------


def test_policy_map_matches_the_live_schema(initialized_db: str):
    """A future table with a user column fails here until someone
    writes its rule."""
    with cursor() as c:
        discovered = set(discover_user_scoped_tables(c))
    assert discovered == set(POLICY_TABLES)


def test_discovery_finds_the_tables_with_no_declared_fk(initialized_db: str):
    with cursor() as c:
        discovered = discover_user_scoped_tables(c)
    assert discovered["notifications_log"] == {"user_id"}
    assert discovered["active_workflows"] == {"user_login"}
    assert discovered["offline_sync_idempotency"] == {"user_id"}
    # The audit table names two user ids and is never moved.
    assert "account_merges" not in discovered


def test_discovery_finds_every_user_column_on_one_table(initialized_db: str):
    """A second owning column must not hide behind the first: half a
    table's rows would cascade away with both guards reporting clean."""
    with cursor() as c:
        c.execute(
            "CREATE TABLE deck_shares (id INTEGER PRIMARY KEY,"
            " user_id TEXT NOT NULL REFERENCES users(tailscale_login) ON DELETE CASCADE,"
            " shared_with TEXT NOT NULL REFERENCES users(tailscale_login) ON DELETE CASCADE)"
        )
        discovered = discover_user_scoped_tables(c)
    assert discovered["deck_shares"] == {"user_id", "shared_with"}


def test_boot_guard_rejects_a_second_user_column_on_a_covered_table(initialized_db: str):
    from prep.infrastructure import db as db_mod

    with cursor() as c:
        c.execute(
            "ALTER TABLE decks ADD COLUMN shared_with TEXT"
            " REFERENCES users(tailscale_login) ON DELETE CASCADE"
        )
    with pytest.raises(UnknownUserScopedTable) as excinfo:
        db_mod.init()
    assert "decks.shared_with" in str(excinfo.value)


def test_cascade_guard_counts_every_user_column(initialized_db: str):
    """The anonymous account is the RECIPIENT of a third party's row.
    Its own column is empty, so a one-column guard passes and the
    delete destroys the row."""
    target = initialized_db
    seed_anon_user(ANON)
    seed_all_tables(ANON, "anon")
    with cursor() as c:
        c.execute(
            "INSERT INTO users (tailscale_login, created_at, last_seen_at)"
            " VALUES ('third@example.com', '2026-01-01T00:00:00+00:00',"
            " '2026-01-01T00:00:00+00:00')"
        )
        c.execute(
            "CREATE TABLE deck_shares (id INTEGER PRIMARY KEY,"
            " user_id TEXT NOT NULL REFERENCES users(tailscale_login) ON DELETE CASCADE,"
            " shared_with TEXT NOT NULL REFERENCES users(tailscale_login) ON DELETE CASCADE)"
        )
        c.execute(
            "INSERT INTO deck_shares (user_id, shared_with) VALUES ('third@example.com', ?)",
            (ANON,),
        )

    with pytest.raises(LeftoverAnonRows, match="deck_shares.shared_with"):
        merge_anonymous_into(ANON, target)

    assert user_exists(ANON)
    assert count_for("deck_shares", "shared_with", ANON) == 1
    assert count_for("decks", "user_id", ANON) == 1


def test_init_raises_on_an_uncovered_user_scoped_table(initialized_db: str):
    from prep.infrastructure import db as db_mod

    with cursor() as c:
        c.execute("CREATE TABLE future_feature (id INTEGER PRIMARY KEY, user_id TEXT NOT NULL)")
    with pytest.raises(UnknownUserScopedTable) as excinfo:
        db_mod.init()
    assert "future_feature" in str(excinfo.value)


def test_users_columns_match_the_disposition_table(initialized_db: str):
    """Table discovery covers tables, not columns: a new `users`
    column is invisible to it and only this catches it."""
    with cursor() as c:
        columns = {r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()}
    assert columns == set(CARRIED_USER_COLUMNS) | set(DROPPED_USER_COLUMNS)


# ---- the policy map ---------------------------------------------------


def test_every_policy_table_moves_or_drops(initialized_db: str):
    target = initialized_db
    seed_anon_user(ANON)
    seed_all_tables(ANON, "anon")
    seed_anon_user(OTHER_ANON)  # stands in as an unrelated third account
    seed_all_tables(OTHER_ANON, "third")

    result = merge_anonymous_into(ANON, target)

    assert result.resolved and result.merged
    for rule in POLICY:
        assert count_for(rule.table, rule.column, ANON) == 0, rule.table
        expected = 0 if rule.rule == DELETE else 1
        assert count_for(rule.table, rule.column, target) == expected, rule.table
        # The unrelated account keeps everything it owns.
        assert count_for(rule.table, rule.column, OTHER_ANON) == 1, rule.table
    assert not user_exists(ANON)
    assert user_exists(OTHER_ANON)


def test_counts_report_what_moved(initialized_db: str):
    target = initialized_db
    seed_anon_user(ANON)
    seed_all_tables(ANON, "anon")

    result = merge_anonymous_into(ANON, target)

    assert result.counts["decks"] == 1
    assert result.counts["questions"] == 1
    assert result.counts["active_workflows"] == 1
    assert result.counts["push_subscriptions"] == 1
    stored = json.loads(audit_rows()[-1]["counts"])
    assert stored == result.counts


def test_secrets_are_deleted_not_reassigned(initialized_db: str):
    """Seeded by direct SQL, bypassing the capability gate: a key or a
    token must never change identities."""
    target = initialized_db
    seed_anon_user(ANON)
    seed_all_tables(ANON, "anon")

    merge_anonymous_into(ANON, target)

    for table in ("byok_credentials", "api_tokens", "push_subscriptions"):
        assert count_for(table, "user_id", ANON) == 0
        assert count_for(table, "user_id", target) == 0


def test_derived_tables_follow_their_parents(initialized_db: str, sql_trace: list[str]):
    target = initialized_db
    seed_anon_user(ANON)
    seed_all_tables(ANON, "anon")

    merge_anonymous_into(ANON, target)

    with cursor() as c:
        card = c.execute(
            "SELECT c.next_due, c.stability FROM cards c"
            " JOIN questions q ON q.id = c.question_id WHERE q.user_id = ?",
            (target,),
        ).fetchone()
        reviews = c.execute(
            "SELECT COUNT(*) AS n FROM reviews r"
            " JOIN questions q ON q.id = r.question_id WHERE q.user_id = ?",
            (target,),
        ).fetchone()["n"]
        answers = c.execute("SELECT COUNT(*) AS n FROM study_session_answers").fetchone()["n"]
        queued = c.execute("SELECT COUNT(*) AS n FROM trivia_queue").fetchone()["n"]
    assert card["next_due"] == "2026-06-01T00:00:00+00:00"
    assert card["stability"] == 7.5
    assert reviews == 1
    assert answers == 1
    assert queued == 1
    writes = [
        s for s in sql_trace if s.strip().split()[0].upper() in {"UPDATE", "DELETE", "INSERT"}
    ]
    assert any('UPDATE "questions"' in s for s in writes), "trace captured nothing"
    for table in DERIVED_TABLES:
        assert not any(table in s for s in writes), table


def test_offline_sync_idempotency_conflicts_drop(initialized_db: str):
    """A target row for the same client_id already records an outcome
    and wins; the rest of the anonymous rows still move."""
    target = initialized_db
    seed_anon_user(ANON)
    with cursor() as c:
        for user, client, status in (
            (ANON, "shared", "created"),
            (ANON, "anon-only", "applied"),
            (target, "shared", "applied"),
        ):
            c.execute(
                "INSERT INTO offline_sync_idempotency"
                " (user_id, client_id, kind, status, created_at)"
                " VALUES (?, ?, 'review', ?, '2026-01-01T00:00:00+00:00')",
                (user, client, status),
            )

    result = merge_anonymous_into(ANON, target)

    with cursor() as c:
        rows = {
            r["client_id"]: r["status"]
            for r in c.execute(
                "SELECT client_id, status FROM offline_sync_idempotency WHERE user_id = ?",
                (target,),
            ).fetchall()
        }
    assert rows == {"shared": "applied", "anon-only": "applied"}
    assert result.counts["offline_sync_idempotency.dropped"] == 1


# ---- the users row ----------------------------------------------------


def test_preferences_carry_over_when_the_target_has_none(initialized_db: str):
    target = initialized_db
    seed_anon_user(ANON)
    with cursor() as c:
        c.execute(
            "UPDATE users SET desired_retention = 0.95, editor_input_mode = 'vim'"
            " WHERE tailscale_login = ?",
            (ANON,),
        )

    result = merge_anonymous_into(ANON, target)

    with cursor() as c:
        row = c.execute(
            "SELECT desired_retention, editor_input_mode FROM users WHERE tailscale_login = ?",
            (target,),
        ).fetchone()
    assert row["desired_retention"] == 0.95
    assert row["editor_input_mode"] == "vim"
    assert result.counts["users.desired_retention"] == 1
    assert result.counts["users.editor_input_mode"] == 1
    assert not user_exists(ANON)


def test_preferences_never_overwrite_the_targets_own(initialized_db: str):
    target = initialized_db
    seed_anon_user(ANON)
    with cursor() as c:
        c.execute(
            "UPDATE users SET desired_retention = 0.95, editor_input_mode = 'vim'"
            " WHERE tailscale_login = ?",
            (ANON,),
        )
        c.execute(
            "UPDATE users SET desired_retention = 0.80, editor_input_mode = 'emacs'"
            " WHERE tailscale_login = ?",
            (target,),
        )

    result = merge_anonymous_into(ANON, target)

    with cursor() as c:
        row = c.execute(
            "SELECT desired_retention, editor_input_mode FROM users WHERE tailscale_login = ?",
            (target,),
        ).fetchone()
    assert row["desired_retention"] == 0.80
    assert row["editor_input_mode"] == "emacs"
    assert "users.desired_retention" not in result.counts
    assert "users.editor_input_mode" not in result.counts


def test_merged_cards_keep_the_schedule_their_retention_computed(initialized_db: str):
    """The scheduling consequence of the carry-over: intervals
    computed at one retention target are not extended at another."""
    target = initialized_db
    seed_anon_user(ANON)
    seed_all_tables(ANON, "anon")
    with cursor() as c:
        c.execute(
            "UPDATE users SET desired_retention = 0.95 WHERE tailscale_login = ?",
            (ANON,),
        )

    merge_anonymous_into(ANON, target)

    from prep.auth.repo import UserRepo

    assert UserRepo().get_desired_retention(target) == 0.95
    with cursor() as c:
        card = c.execute(
            "SELECT next_due, step, fsrs_state FROM cards c"
            " JOIN questions q ON q.id = c.question_id WHERE q.user_id = ?",
            (target,),
        ).fetchone()
    assert card["next_due"] == "2026-06-01T00:00:00+00:00"
    assert card["step"] == 2
    assert card["fsrs_state"] == 2


# ---- slugs ------------------------------------------------------------


def _decks(user_id: str) -> dict[str, str]:
    with cursor() as c:
        return {
            r["name"]: r["display_name"]
            for r in c.execute(
                "SELECT name, display_name FROM decks WHERE user_id = ?", (user_id,)
            ).fetchall()
        }


def test_slug_collision_keeps_both_decks(initialized_db: str):
    target = initialized_db
    seed_anon_user(ANON)
    with cursor() as c:
        for user in (target, ANON):
            c.execute(
                "INSERT INTO decks (user_id, name, display_name, created_at)"
                " VALUES (?, 'french-revolution', 'French Revolution',"
                " '2026-01-01T00:00:00+00:00')",
                (user,),
            )

    merge_anonymous_into(ANON, target)

    decks = _decks(target)
    assert set(decks) == {"french-revolution", "french-revolution-2"}
    assert set(decks.values()) == {"French Revolution"}


def test_slug_exhaustion_does_not_wedge_the_merge(initialized_db: str):
    target = initialized_db
    seed_anon_user(ANON)
    with cursor() as c:
        c.execute(
            "INSERT INTO decks (user_id, name, created_at)"
            " VALUES (?, 'x', '2026-01-01T00:00:00+00:00')",
            (target,),
        )
        for n in range(2, 101):
            c.execute(
                "INSERT INTO decks (user_id, name, created_at)"
                " VALUES (?, ?, '2026-01-01T00:00:00+00:00')",
                (target, f"x-{n}"),
            )
        c.execute(
            "INSERT INTO decks (user_id, name, created_at)"
            " VALUES (?, 'x', '2026-01-01T00:00:00+00:00')",
            (ANON,),
        )

    result = merge_anonymous_into(ANON, target)

    assert result.merged
    names = set(_decks(target))
    assert len(names) == 101
    fresh = names - {"x"} - {f"x-{n}" for n in range(2, 101)}
    assert len(fresh) == 1
    assert fresh.pop().startswith("x-")


def test_a_deck_colliding_with_the_anon_accounts_own_suffix(initialized_db: str):
    """The rename clears the anonymous account's UNIQUE first, so a
    suffix it already uses is not a candidate."""
    target = initialized_db
    seed_anon_user(ANON)
    with cursor() as c:
        c.execute(
            "INSERT INTO decks (user_id, name, created_at)"
            " VALUES (?, 'x', '2026-01-01T00:00:00+00:00')",
            (target,),
        )
        for name in ("x", "x-2"):
            c.execute(
                "INSERT INTO decks (user_id, name, created_at)"
                " VALUES (?, ?, '2026-01-01T00:00:00+00:00')",
                (ANON, name),
            )

    assert merge_anonymous_into(ANON, target).merged
    assert set(_decks(target)) == {"x", "x-2", "x-3"}


# ---- preconditions ----------------------------------------------------


def test_anon_row_absent_is_resolved(initialized_db: str):
    result = merge_anonymous_into(ANON, initialized_db)
    assert result.resolved
    assert not result.merged
    assert result.reason == "anon_missing"
    assert audit_rows()[-1]["status"] == "failed"


def test_same_user_is_resolved_without_an_audit_row(initialized_db: str):
    result = merge_anonymous_into(initialized_db, initialized_db)
    assert result.resolved
    assert not result.merged
    assert result.reason == "same_user"
    assert audit_rows() == []


def test_a_non_anonymous_row_is_not_resolved(initialized_db: str):
    """Should be unreachable: the cookie names a row whose flag was
    cleared, so the merge refuses and the cookie is kept."""
    from prep.auth.repo import UserRepo

    UserRepo().upsert(ANON, display_name="Not a guest")
    seed_all_tables(ANON, "anon")

    result = merge_anonymous_into(ANON, initialized_db)

    assert not result.resolved
    assert result.reason == "not_anonymous"
    assert user_exists(ANON)
    assert count_for("decks", "user_id", ANON) == 1


def test_target_row_absent_is_not_resolved(initialized_db: str):
    seed_anon_user(ANON)
    seed_all_tables(ANON, "anon")

    result = merge_anonymous_into(ANON, "nobody@example.com")

    assert not result.resolved
    assert result.reason == "target_missing"
    assert user_exists(ANON)
    assert count_for("decks", "user_id", ANON) == 1


def test_a_kept_cookie_refusal_writes_no_audit_row(initialized_db: str):
    """Both refusals keep the cookie, so the browser re-presents it on
    every request. Auditing an attempt that writes nothing else would
    grow the table without bound."""
    seed_anon_user(ANON)

    for _ in range(20):
        assert merge_anonymous_into(ANON, "nobody@example.com").reason == "target_missing"
    assert audit_rows() == []

    with cursor() as c:
        c.execute("UPDATE users SET is_anonymous = 0 WHERE tailscale_login = ?", (ANON,))
    for _ in range(20):
        assert merge_anonymous_into(ANON, initialized_db).reason == "not_anonymous"
    assert audit_rows() == []


# ---- idempotency, atomicity, guards -----------------------------------


def test_re_running_after_success_is_a_no_op(initialized_db: str):
    target = initialized_db
    seed_anon_user(ANON)
    seed_all_tables(ANON, "anon")

    first = merge_anonymous_into(ANON, target)
    before = {rule.table: count_for(rule.table, rule.column, target) for rule in POLICY}
    second = merge_anonymous_into(ANON, target)
    after = {rule.table: count_for(rule.table, rule.column, target) for rule in POLICY}

    assert first.merged and not second.merged
    assert second.resolved and second.reason == "anon_missing"
    assert before == after
    rows = audit_rows()
    assert len(rows) == 2
    assert [r["status"] for r in rows] == ["completed", "failed"]


def test_partial_failure_rolls_back(initialized_db: str, monkeypatch: pytest.MonkeyPatch):
    target = initialized_db
    seed_anon_user(ANON)
    deck_id = seed_all_tables(ANON, "anon")
    real = merge_mod._apply

    def failing(c, rule, anon_user_id, target_user_id, counts):
        if rule.table == "instant_generations":
            raise RuntimeError("injected")
        return real(c, rule, anon_user_id, target_user_id, counts)

    monkeypatch.setattr(merge_mod, "_apply", failing)

    with pytest.raises(RuntimeError, match="injected"):
        merge_anonymous_into(ANON, target)

    assert user_exists(ANON)
    for rule in POLICY:
        assert count_for(rule.table, rule.column, ANON) == 1, rule.table
        assert count_for(rule.table, rule.column, target) == 0, rule.table
    row = audit_rows()[-1]
    assert row["status"] == "started"
    assert row["completed_at"] is None

    monkeypatch.setattr(merge_mod, "_apply", real)
    assert merge_anonymous_into(ANON, target).merged
    with cursor() as c:
        owner = c.execute("SELECT user_id FROM decks WHERE id = ?", (deck_id,)).fetchone()
    assert owner["user_id"] == target


def test_cascade_guard_refuses_an_uncovered_table(initialized_db: str):
    """A table the policy map does not cover holds anonymous rows, so
    the `users` delete would destroy them. Nothing is written."""
    target = initialized_db
    seed_anon_user(ANON)
    seed_all_tables(ANON, "anon")
    with cursor() as c:
        c.execute(
            "CREATE TABLE future_feature (id INTEGER PRIMARY KEY,"
            " user_id TEXT NOT NULL REFERENCES users(tailscale_login) ON DELETE CASCADE)"
        )
        c.execute("INSERT INTO future_feature (user_id) VALUES (?)", (ANON,))

    with pytest.raises(LeftoverAnonRows, match="future_feature"):
        merge_anonymous_into(ANON, target)

    assert user_exists(ANON)
    assert count_for("decks", "user_id", ANON) == 1
    assert count_for("decks", "user_id", target) == 0
    assert count_for("future_feature", "user_id", ANON) == 1


def test_cascade_guard_catches_a_rule_that_moved_nothing(
    initialized_db: str, monkeypatch: pytest.MonkeyPatch
):
    target = initialized_db
    seed_anon_user(ANON)
    seed_all_tables(ANON, "anon")
    real = merge_mod._apply

    def skipping(c, rule, anon_user_id, target_user_id, counts):
        if rule.table == "notifications_log":
            return None
        return real(c, rule, anon_user_id, target_user_id, counts)

    monkeypatch.setattr(merge_mod, "_apply", skipping)

    with pytest.raises(LeftoverAnonRows, match="notifications_log"):
        merge_anonymous_into(ANON, target)

    assert user_exists(ANON)
    assert count_for("decks", "user_id", ANON) == 1


# ---- more than two accounts -------------------------------------------


def test_one_cookie_signing_into_two_accounts(initialized_db: str):
    """The second account gets nothing: the anonymous row is gone, and
    the answer is the same resolved no-op a reap would give."""
    from prep.auth.repo import UserRepo

    first = initialized_db
    second = UserRepo().upsert("second@example.com", display_name="Second")["tailscale_login"]
    seed_anon_user(ANON)
    seed_all_tables(ANON, "anon")

    assert merge_anonymous_into(ANON, first).merged
    result = merge_anonymous_into(ANON, second)

    assert result.resolved and not result.merged
    assert result.reason == "anon_missing"
    assert count_for("decks", "user_id", first) == 1
    assert count_for("decks", "user_id", second) == 0
    assert previous_user_ids(second) == []


def test_two_cookies_into_one_account(initialized_db: str):
    target = initialized_db
    seed_anon_user(ANON)
    seed_all_tables(ANON, "first")
    seed_anon_user(OTHER_ANON)
    seed_all_tables(OTHER_ANON, "second")

    assert merge_anonymous_into(ANON, target).merged
    assert merge_anonymous_into(OTHER_ANON, target).merged

    assert count_for("decks", "user_id", target) == 2
    assert count_for("questions", "user_id", target) == 2
    assert previous_user_ids(target) == [ANON, OTHER_ANON]
    assert not user_exists(ANON)
    assert not user_exists(OTHER_ANON)


def test_previous_ids_only_lists_completed_merges(initialized_db: str):
    target = initialized_db
    merge_anonymous_into(ANON, target)  # anon_missing -> failed
    assert previous_user_ids(target) == []
    seed_anon_user(ANON)
    assert merge_anonymous_into(ANON, target).merged
    assert previous_user_ids(target) == [ANON]


# ---- concurrency ------------------------------------------------------


def _race(fn, count: int = 2) -> tuple[list, list]:
    """Run `fn(i)` on `count` threads released together by a barrier."""
    barrier = threading.Barrier(count)
    results: list = []
    errors: list = []
    guard = threading.Lock()

    def run(i: int) -> None:
        barrier.wait()
        try:
            outcome = fn(i)
        except Exception as exc:  # noqa: BLE001 - the assertion is that none escape
            with guard:
                errors.append(exc)
            return
        with guard:
            results.append(outcome)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)
    return results, errors


@pytest.fixture
def slow_move(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hold the write transaction open long enough that both threads
    are inside `merge_anonymous_into` at once. Without it the race
    window is sub-millisecond and a lost `BEGIN IMMEDIATE` passes."""
    real = merge_mod._move

    def delayed(*args, **kwargs):
        time.sleep(0.05)
        return real(*args, **kwargs)

    monkeypatch.setattr(merge_mod, "_move", delayed)


@pytest.mark.parametrize("trial", range(5))
def test_two_threads_one_cookie_one_target(initialized_db: str, slow_move: None, trial: int):
    target = initialized_db
    seed_anon_user(ANON)
    seed_all_tables(ANON, "anon")

    results, errors = _race(lambda _: merge_anonymous_into(ANON, target))

    assert errors == []
    assert sorted(r.merged for r in results) == [False, True]
    assert all(r.resolved for r in results)
    for rule in POLICY:
        if rule.rule is DELETE:
            continue
        assert count_for(rule.table, rule.column, target) == 1, rule.table
    assert not user_exists(ANON)
    assert previous_user_ids(target) == [ANON]


@pytest.mark.parametrize("trial", range(5))
def test_two_threads_one_cookie_two_targets(initialized_db: str, slow_move: None, trial: int):
    """The only shape that can catch a split brain: decks landing on
    one account while questions land on another."""
    from prep.auth.repo import UserRepo

    targets = [
        initialized_db,
        UserRepo().upsert("second@example.com", display_name="Second")["tailscale_login"],
    ]
    seed_anon_user(ANON)
    seed_all_tables(ANON, "anon")

    results, errors = _race(lambda i: merge_anonymous_into(ANON, targets[i]))

    assert errors == []
    assert sorted(r.merged for r in results) == [False, True]
    owners = {
        table: [t for t in targets if count_for(table, "user_id", t)]
        for table in ("decks", "questions", "study_sessions", "trivia_sessions")
    }
    assert list(owners.values()).count(owners["decks"]) == len(owners)
    assert len(owners["decks"]) == 1
    assert not user_exists(ANON)
