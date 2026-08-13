"""`optional_current_user` on an anonymous identity, and the repo
method it leans on."""

from __future__ import annotations

import pytest

from prep.auth import anon_cookie as ac
from prep.auth.identity import optional_current_user
from prep.auth.providers import set_provider
from prep.auth.providers.anon import AnonymousFallbackProvider
from prep.auth.repo import UserRepo
from prep.infrastructure.db import cursor
from tests.auth.test_anon_provider import _Inner, make_request

EXTERNAL_ID = "anon:" + "ab" * 16
MASTER = "11" * 32


@pytest.fixture
def secret(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(ac.SECRET_ENV, raising=False)
    monkeypatch.setenv(ac.MASTER_ENV, MASTER)


@pytest.fixture
def anon_provider(secret):
    set_provider(AnonymousFallbackProvider(_Inner()))
    yield
    set_provider(None)


def seed_anon_user(external_id: str = EXTERNAL_ID, *, last_seen: str = "2000-01-01T00:00:00+00:00"):
    with cursor() as c:
        c.execute(
            """INSERT INTO users (tailscale_login, display_name, email, created_at,
                                  last_seen_at, is_anonymous)
               VALUES (?, 'Guest', NULL, ?, ?, 1)""",
            (external_id, last_seen, last_seen),
        )


def count_users() -> int:
    with cursor() as c:
        return c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


def test_touch_bumps_last_seen_and_inserts_nothing(initialized_db: str):
    seed_anon_user()
    before = count_users()
    UserRepo().touch(EXTERNAL_ID)
    with cursor() as c:
        row = c.execute(
            "SELECT last_seen_at FROM users WHERE tailscale_login = ?", (EXTERNAL_ID,)
        ).fetchone()
    assert row["last_seen_at"] > "2000-01-01"
    assert count_users() == before

    UserRepo().touch("anon:" + "ff" * 16)
    assert count_users() == before


def test_anonymous_cookie_resolves_the_existing_row(initialized_db: str, anon_provider):
    seed_anon_user()
    request = make_request({ac.COOKIE_NAME: ac.mint_cookie(EXTERNAL_ID)})
    user = optional_current_user(request)
    assert user is not None
    assert user["tailscale_login"] == EXTERNAL_ID
    assert user["is_anonymous"] == 1
    assert request.state.user == user
    with cursor() as c:
        row = c.execute(
            "SELECT last_seen_at FROM users WHERE tailscale_login = ?", (EXTERNAL_ID,)
        ).fetchone()
    assert row["last_seen_at"] > "2000-01-01"


def test_stale_cookie_does_not_resurrect_a_deleted_row(initialized_db: str, anon_provider):
    """The regression the anonymous branch exists to prevent: upsert
    inserts on miss, so routing an anonymous id through it would
    recreate a reaped account as an empty user forever."""
    before = count_users()
    request = make_request({ac.COOKIE_NAME: ac.mint_cookie(EXTERNAL_ID)})
    assert optional_current_user(request) is None
    assert request.state.anon_cookie_stale is True
    assert count_users() == before
    with cursor() as c:
        assert (
            c.execute("SELECT 1 FROM users WHERE tailscale_login = ?", (EXTERNAL_ID,)).fetchone()
            is None
        )


def test_a_row_that_is_no_longer_anonymous_makes_the_cookie_dead(
    initialized_db: str, anon_provider
):
    """Every downstream gate reads the ROW's flag, so the resolver has
    to agree with it. A row keyed `anon:<hex>` without the flag would
    otherwise hand the cookie bearer an unrestricted session: PAT
    minting, BYOK storage, push subscriptions."""
    seed_anon_user()
    with cursor() as c:
        c.execute("UPDATE users SET is_anonymous = 0 WHERE tailscale_login = ?", (EXTERNAL_ID,))
    request = make_request({ac.COOKIE_NAME: ac.mint_cookie(EXTERNAL_ID)})
    assert optional_current_user(request) is None
    assert request.state.anon_cookie_stale is True


def test_signed_in_user_still_upserts(initialized_db: str, secret):
    from prep.auth.port import ResolvedUser

    signed_in = ResolvedUser(
        external_id="user_2abc",
        email="a@example.com",
        display_name="A",
        profile_pic_url=None,
        provider="inner",
    )
    set_provider(AnonymousFallbackProvider(_Inner(user=signed_in)))
    try:
        request = make_request({ac.COOKIE_NAME: ac.mint_cookie(EXTERNAL_ID)})
        user = optional_current_user(request)
    finally:
        set_provider(None)
    assert user is not None
    assert user["tailscale_login"] == "user_2abc"
    assert user["is_anonymous"] == 0
    with cursor() as c:
        assert (
            c.execute("SELECT 1 FROM users WHERE tailscale_login = ?", (EXTERNAL_ID,)).fetchone()
            is None
        )


def test_anonymous_rows_are_marked_by_column_and_id(initialized_db: str):
    seed_anon_user()
    with cursor() as c:
        row = c.execute(
            "SELECT tailscale_login, is_anonymous FROM users WHERE tailscale_login = ?",
            (EXTERNAL_ID,),
        ).fetchone()
        provider_row = c.execute(
            "SELECT is_anonymous FROM users WHERE tailscale_login = ?", (initialized_db,)
        ).fetchone()
    assert row["tailscale_login"].startswith("anon:")
    assert row["is_anonymous"] == 1
    # The column defaults to 0, so every provider row stays non-anonymous.
    assert provider_row["is_anonymous"] == 0
