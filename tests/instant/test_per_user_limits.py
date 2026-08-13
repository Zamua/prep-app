"""The per-user daily windows on the instant ledger
(docs/ANONYMOUS-ACCOUNTS.md section 6).

Timestamps are injected, never slept. Each reservation steps 61s so
the per-IP burst and the global per-minute cap stay clear, and the
IP rotates so the per-IP daily window never decides the outcome
unless a test is about it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from prep.auth.merge import merge_anonymous_into
from prep.infrastructure.db import cursor
from prep.instant import repo
from tests.anon_support import seed_anon_user, seed_named_user

T0 = datetime(2026, 1, 5, 12, 0, 0, tzinfo=timezone.utc)

ANON = "anon:" + "ab" * 16
NAMED = "user_2abc"
UNKNOWN = "anon:" + "ff" * 16


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _ip(n: int) -> str:
    return f"198.51.100.{n}"


def _reserve(step: int, *, user_id: str | None, ip: str | None = None):
    """One admission attempt, `step` slots into the timeline."""
    return repo.check_and_reserve(
        ip or _ip(step + 1), topic_chars=10, user_id=user_id, at=_at(step * 61)
    )


def _spend(step: int, *, user_id: str | None, ip: str | None = None) -> None:
    """An admitted attempt resolved as spend."""
    gate = _reserve(step, user_id=user_id, ip=ip)
    assert isinstance(gate, repo.Reservation), gate
    repo.resolve(gate.id, "ok", cards=5, user_id=user_id)


# ---- the per-user daily window ----------------------------------------


def test_the_anonymous_daily_window_refuses_at_the_cap(initialized_db: str):
    seed_anon_user(ANON)
    for step in range(repo.DEFAULT_PER_ANON_USER_PER_DAY):
        _spend(step, user_id=ANON)

    refusal = _reserve(repo.DEFAULT_PER_ANON_USER_PER_DAY, user_id=ANON)

    assert isinstance(refusal, repo.Refusal)
    assert refusal.kind == "day"
    assert refusal.retry_after_s == repo.DAY_WINDOW_S - 3 * 61


def test_a_signed_in_user_keeps_going_past_the_anonymous_cap(initialized_db: str):
    seed_named_user(NAMED)
    for step in range(repo.DEFAULT_PER_ANON_USER_PER_DAY + 1):
        _spend(step, user_id=NAMED)

    with cursor() as c:
        assert repo._user_day_limit(c, NAMED) == repo.DEFAULT_PER_USER_PER_DAY


def test_a_fresh_ip_does_not_buy_a_fresh_user_budget(initialized_db: str):
    # Every attempt comes from a different IP, so only the per-user
    # window can be what refuses.
    seed_anon_user(ANON)
    for step in range(repo.DEFAULT_PER_ANON_USER_PER_DAY):
        _spend(step, user_id=ANON)

    refusal = _reserve(9, user_id=ANON, ip="203.0.113.77")

    assert isinstance(refusal, repo.Refusal)
    assert refusal.kind == "day"


def test_a_fresh_cookie_does_not_buy_a_fresh_ip_budget(initialized_db: str):
    # Every attempt names a different account from one IP, so only the
    # per-IP window can be what refuses.
    shared = "198.51.100.200"
    for step in range(repo.DEFAULT_PER_IP_PER_DAY):
        user_id = seed_anon_user(f"anon:{step:032x}")
        _spend(step, user_id=user_id, ip=shared)

    refusal = _reserve(9, user_id=seed_anon_user("anon:" + "ee" * 16), ip=shared)

    assert isinstance(refusal, repo.Refusal)
    assert refusal.kind == "day"


def test_both_windows_admit_together(initialized_db: str):
    seed_anon_user(ANON)
    _spend(0, user_id=ANON)

    assert isinstance(_reserve(1, user_id=ANON), repo.Reservation)


def test_rows_outside_the_day_window_do_not_count(initialized_db: str):
    seed_anon_user(ANON)
    for step in range(repo.DEFAULT_PER_ANON_USER_PER_DAY):
        _spend(step, user_id=ANON)

    late = repo.check_and_reserve(
        "203.0.113.5", topic_chars=10, user_id=ANON, at=_at(repo.DAY_WINDOW_S + 1)
    )

    assert isinstance(late, repo.Reservation)


def test_free_refusals_do_not_count_toward_the_user_window(initialized_db: str):
    seed_anon_user(ANON)
    for step in range(repo.DEFAULT_PER_ANON_USER_PER_DAY):
        gate = _reserve(step, user_id=ANON)
        assert isinstance(gate, repo.Reservation), gate
        repo.resolve(gate.id, "failed_free", user_id=ANON)

    assert isinstance(_reserve(4, user_id=ANON), repo.Reservation)


def test_an_unknown_user_id_takes_the_tighter_limit(initialized_db: str):
    with cursor() as c:
        assert repo._user_day_limit(c, UNKNOWN) == repo.DEFAULT_PER_ANON_USER_PER_DAY


def test_the_env_knobs_move_both_limits(initialized_db: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PREP_INSTANT_PER_ANON_USER_PER_DAY", "1")
    monkeypatch.setenv("PREP_INSTANT_PER_USER_PER_DAY", "2")
    seed_anon_user(ANON)
    seed_named_user(NAMED)
    with cursor() as c:
        assert repo._user_day_limit(c, ANON) == 1
        assert repo._user_day_limit(c, NAMED) == 2


# ---- the request that mints the account --------------------------------


def test_the_minting_request_reserves_with_no_id_and_is_back_stamped(initialized_db: str):
    """The account does not exist at reserve time, so its first spend
    is attributed by `resolve` and counts from that moment."""
    gate = _reserve(0, user_id=None)
    assert isinstance(gate, repo.Reservation)
    seed_anon_user(ANON)
    repo.resolve(gate.id, "ok", cards=5, user_id=ANON)

    with cursor() as c:
        counted = c.execute(
            "SELECT COUNT(*) AS n FROM instant_generations WHERE user_id = ?", (ANON,)
        ).fetchone()["n"]
    assert counted == 1

    # The second generation sees that row: two more spends reach the
    # cap of three, and the fourth attempt is refused.
    _spend(1, user_id=ANON)
    _spend(2, user_id=ANON)
    refusal = _reserve(3, user_id=ANON)
    assert isinstance(refusal, repo.Refusal)
    assert refusal.kind == "day"


def test_an_unstamped_row_is_not_charged_to_anyone(initialized_db: str):
    """The control for the test above: without the back-stamp the
    account's window starts empty, which is the bug the stamp fixes."""
    gate = _reserve(0, user_id=None)
    seed_anon_user(ANON)
    repo.resolve(gate.id, "ok", cards=5)

    _spend(1, user_id=ANON)
    _spend(2, user_id=ANON)

    assert isinstance(_reserve(3, user_id=ANON), repo.Reservation)


# ---- the window follows the person -------------------------------------


def test_spend_follows_an_anonymous_user_through_a_merge(
    initialized_db: str, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("PREP_INSTANT_PER_USER_PER_DAY", "3")
    seed_anon_user(ANON)
    seed_named_user(NAMED)
    for step in range(3):
        _spend(step, user_id=ANON)

    assert merge_anonymous_into(ANON, NAMED).merged

    refusal = _reserve(9, user_id=NAMED)
    assert isinstance(refusal, repo.Refusal)
    assert refusal.kind == "day"
