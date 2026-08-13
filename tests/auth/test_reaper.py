"""`reap_idle_anonymous`: what it selects, what it spares, and the
bounds it runs under (docs/ANONYMOUS-ACCOUNTS.md section 6).

The seeding helpers come from the merge suite so both destructive
paths are proven against the same set of user-owned tables.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from datetime import datetime, timedelta, timezone

import pytest

from prep.auth import anon_cookie, reaper
from prep.auth.merge import POLICY
from prep.auth.repo import UserRepo
from prep.infrastructure import db as infra_db
from prep.infrastructure.db import cursor
from tests.auth.test_merge import count_for, seed_all_tables, user_exists

NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)

ANON = "anon:" + "ab" * 16
OTHER_ANON = "anon:" + "cd" * 16
NAMED = "user_2abc"


def _stamp(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def seed_user(user_id: str, *, last_seen: str, anonymous: bool = True) -> str:
    with cursor() as c:
        c.execute(
            "INSERT INTO users (tailscale_login, display_name, email, created_at,"
            " last_seen_at, is_anonymous) VALUES (?, 'Guest', NULL, ?, ?, ?)",
            (user_id, last_seen, last_seen, 1 if anonymous else 0),
        )
    return user_id


def policy_counts(user_id: str) -> dict[str, int]:
    return {rule.table: count_for(rule.table, rule.column, user_id) for rule in POLICY}


def derived_counts(deck_id: int) -> dict[str, int]:
    with cursor() as c:
        cards = c.execute(
            "SELECT COUNT(*) AS n FROM cards WHERE question_id IN"
            " (SELECT id FROM questions WHERE deck_id = ?)",
            (deck_id,),
        ).fetchone()["n"]
        reviews = c.execute(
            "SELECT COUNT(*) AS n FROM reviews WHERE question_id IN"
            " (SELECT id FROM questions WHERE deck_id = ?)",
            (deck_id,),
        ).fetchone()["n"]
    return {"cards": cards, "reviews": reviews}


# ---- selection --------------------------------------------------------


def test_an_idle_anonymous_account_goes_with_all_its_rows(initialized_db: str):
    seed_user(ANON, last_seen=_stamp(400))
    deck_id = seed_all_tables(ANON, "anon")

    assert reaper.reap_idle_anonymous(NOW) == 1

    assert not user_exists(ANON)
    assert policy_counts(ANON) == dict.fromkeys(policy_counts(ANON), 0)
    # The FK chain carries the tables with no user column of their own.
    assert derived_counts(deck_id) == {"cards": 0, "reviews": 0}


def test_a_recently_active_anonymous_account_survives(initialized_db: str):
    seed_user(ANON, last_seen=_stamp(1))
    seed_all_tables(ANON, "anon")

    assert reaper.reap_idle_anonymous(NOW) == 0

    assert user_exists(ANON)
    assert all(n == 1 for n in policy_counts(ANON).values())


def test_a_signed_in_account_survives_any_amount_of_idleness(initialized_db: str):
    seed_user(NAMED, last_seen=_stamp(4000), anonymous=False)
    seed_all_tables(NAMED, "named")

    assert reaper.reap_idle_anonymous(NOW) == 0

    assert user_exists(NAMED)
    assert all(n == 1 for n in policy_counts(NAMED).values())


def test_only_the_eligible_accounts_rows_are_touched(initialized_db: str):
    seed_user(ANON, last_seen=_stamp(400))
    seed_all_tables(ANON, "idle")
    seed_user(OTHER_ANON, last_seen=_stamp(2))
    seed_all_tables(OTHER_ANON, "active")

    assert reaper.reap_idle_anonymous(NOW) == 1

    assert policy_counts(ANON) == dict.fromkeys(policy_counts(ANON), 0)
    assert all(n == 1 for n in policy_counts(OTHER_ANON).values())


def test_the_tables_with_no_fk_to_users_are_emptied_too(initialized_db: str):
    # The FK chain cannot reach these four, so an explicit delete is
    # the only thing that clears them.
    no_fk = (
        "notifications_log",
        "active_workflows",
        "offline_sync_idempotency",
        "instant_generations",
    )
    seed_user(ANON, last_seen=_stamp(400))
    seed_all_tables(ANON, "anon")
    before = policy_counts(ANON)
    assert all(before[table] == 1 for table in no_fk)

    reaper.reap_idle_anonymous(NOW)

    assert policy_counts(ANON) == dict.fromkeys(before, 0)


# ---- the guard under the write lock -----------------------------------


def test_a_visitor_returning_after_the_batch_was_selected_is_not_reaped(
    initialized_db: str, monkeypatch: pytest.MonkeyPatch
):
    """The batch is selected outside the per-account transaction, so
    the eligibility predicate has to be applied again inside it."""
    seed_user(ANON, last_seen=_stamp(400))
    real = reaper._reap_one

    def returning_visitor(user_id: str, cutoff: str) -> int:
        UserRepo().touch(user_id)
        return real(user_id, cutoff)

    monkeypatch.setattr(reaper, "_reap_one", returning_visitor)

    assert reaper.reap_idle_anonymous(NOW) == 0
    assert user_exists(ANON)


def test_an_account_that_stopped_being_anonymous_is_not_reaped(
    initialized_db: str, monkeypatch: pytest.MonkeyPatch
):
    seed_user(ANON, last_seen=_stamp(400))
    real = reaper._reap_one

    def signs_up(user_id: str, cutoff: str) -> int:
        with cursor() as c:
            c.execute("UPDATE users SET is_anonymous = 0 WHERE tailscale_login = ?", (user_id,))
        return real(user_id, cutoff)

    monkeypatch.setattr(reaper, "_reap_one", signs_up)

    assert reaper.reap_idle_anonymous(NOW) == 0
    assert user_exists(ANON)


def test_the_idle_window_outlives_the_cookie(initialized_db: str):
    """A cookie that still verifies must always name a live row."""
    assert reaper.IDLE_DAYS * 86400 > anon_cookie.MAX_AGE_SECONDS


# ---- the cutoff formatter ---------------------------------------------


def _boundary_row(user_id: str) -> tuple[str, datetime]:
    """Seed a row whose `last_seen_at` was written by `db.now()` at
    exactly the instant the cutoff names."""
    boundary = NOW - timedelta(days=reaper.IDLE_DAYS)

    class _FrozenClock:
        @staticmethod
        def now(tz=None):
            return boundary

    original = infra_db.datetime
    infra_db.datetime = _FrozenClock
    try:
        written = infra_db.now()
    finally:
        infra_db.datetime = original
    seed_user(user_id, last_seen=written)
    return written, boundary


def test_a_row_at_exactly_the_cutoff_survives(initialized_db: str):
    written, _ = _boundary_row(ANON)
    assert written == reaper.cutoff_for(NOW)

    assert reaper.reap_idle_anonymous(NOW) == 0
    assert user_exists(ANON)


def test_a_z_suffixed_cutoff_deletes_the_boundary_row(initialized_db: str):
    """The formatter bug the isoformat rule exists to prevent: 'Z'
    sorts above '+', so the same instant compares as older."""
    _, boundary = _boundary_row(ANON)

    assert reaper._reap_one(ANON, boundary.strftime("%Y-%m-%dT%H:%M:%SZ")) == 1
    assert not user_exists(ANON)


def test_the_cutoff_carries_the_offset_that_now_writes(initialized_db: str):
    assert reaper.cutoff_for(NOW).endswith("+00:00")
    assert infra_db.now().endswith("+00:00")


def test_a_naive_now_is_refused(initialized_db: str):
    with pytest.raises(ValueError):
        reaper.cutoff_for(NOW.replace(tzinfo=None))


# ---- the batch bound --------------------------------------------------


def test_the_batch_bound_holds_and_the_backlog_drains(initialized_db: str):
    for i in range(120):
        seed_user(f"anon:{i:032x}", last_seen=_stamp(400))

    assert reaper.reap_idle_anonymous(NOW) == reaper.BATCH_LIMIT
    assert _anon_count() == 70
    assert reaper.reap_idle_anonymous(NOW) == reaper.BATCH_LIMIT
    assert reaper.reap_idle_anonymous(NOW) == 20
    assert _anon_count() == 0


def test_the_oldest_accounts_go_first(initialized_db: str):
    for i in range(reaper.BATCH_LIMIT + 1):
        seed_user(f"anon:{i:032x}", last_seen=_stamp(400 + i))
    newest = f"anon:{0:032x}"

    reaper.reap_idle_anonymous(NOW)

    assert user_exists(newest)
    assert _anon_count() == 1


def _anon_count() -> int:
    with cursor() as c:
        return c.execute("SELECT COUNT(*) AS n FROM users WHERE is_anonymous = 1").fetchone()["n"]


# ---- off the event loop ------------------------------------------------


async def test_the_sweep_does_not_block_the_event_loop(
    initialized_db: str, monkeypatch: pytest.MonkeyPatch
):
    """A slow reap must not park the loop: a coroutine scheduled
    alongside the tick keeps making progress while it runs."""
    from prep.notify import scheduler as ns

    block_s = 1.0

    def slow_reap(_now):
        time.sleep(block_s)

    monkeypatch.setattr(ns, "_reap_idle_anonymous", slow_reap)

    counter = 0

    async def ticker():
        nonlocal counter
        for _ in range(40):
            await asyncio.sleep(0.05)
            counter += 1

    started = time.monotonic()
    await asyncio.gather(ns._tick(), ticker())
    elapsed = time.monotonic() - started

    assert elapsed < 2.5, f"unexpectedly slow: {elapsed:.2f}s"
    assert counter >= 10, f"counter reached {counter}/40; the loop was parked by the reap"


def test_the_tick_calls_the_reaper_through_to_thread():
    from prep.notify import scheduler as ns

    assert "asyncio.to_thread(_reap_idle_anonymous" in inspect.getsource(ns._tick)
