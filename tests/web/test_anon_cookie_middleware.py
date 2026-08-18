"""The cookie side effects the resolver records and the middleware
emits, driven through the real app: they must reach JSON responses,
not only text/html. Plus the two precedence outcomes visible at
`GET /`, and the rule that a page view mints nothing."""

from __future__ import annotations

import time

import pytest

from prep.auth import anon_cookie as ac
from prep.auth.port import ResolvedUser, SignInUrls
from prep.auth.providers import set_provider
from prep.auth.providers.anon import AnonymousFallbackProvider
from prep.infrastructure.db import cursor

DAY = 86400
EXTERNAL_ID = "anon:" + "ab" * 16
MASTER = "11" * 32
FORGED = "v1.q6urq6urq6urq6urq6urqw.1000.deadbeefdeadbeefdead"


class _Inner:
    name = "stub"

    def __init__(self, *, user: ResolvedUser | None = None, dormant: bool = False):
        self._user = user
        self._dormant = dormant

    def resolve(self, request):
        return self._user

    def urls(self):
        return SignInUrls(sign_in="/sign-in", sign_out=None, account=None)

    def has_dormant_session(self, request):
        return self._dormant


@pytest.fixture
def secret(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(ac.SECRET_ENV, raising=False)
    monkeypatch.setenv(ac.MASTER_ENV, MASTER)


@pytest.fixture
def visitor(secret):
    set_provider(AnonymousFallbackProvider(_Inner()))
    yield
    set_provider(None)


@pytest.fixture
def dormant(secret):
    set_provider(AnonymousFallbackProvider(_Inner(dormant=True)))
    yield
    set_provider(None)


@pytest.fixture
def signed_in(secret):
    set_provider(
        AnonymousFallbackProvider(
            _Inner(
                user=ResolvedUser(
                    external_id="user_2abc",
                    email="a@example.com",
                    display_name="Alice",
                    profile_pic_url=None,
                    provider="stub",
                )
            )
        )
    )
    yield
    set_provider(None)


def seed_anon_user(external_id: str = EXTERNAL_ID):
    with cursor() as c:
        c.execute(
            """INSERT INTO users (tailscale_login, display_name, email, created_at,
                                  last_seen_at, is_anonymous)
               VALUES (?, 'Guest', NULL, ?, ?, 1)""",
            (external_id, "2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00"),
        )


def cookie_header(value: str) -> dict[str, str]:
    """Per-request cookie as a raw header; TestClient's cookies= kwarg is deprecated."""
    return {"cookie": f"{ac.COOKIE_NAME}={value}"}


def anon_set_cookie(response) -> str | None:
    for raw in response.headers.get_list("set-cookie"):
        if raw.startswith(f"{ac.COOKIE_NAME}="):
            return raw
    return None


def user_ids() -> set[str]:
    with cursor() as c:
        return {r["tailscale_login"] for r in c.execute("SELECT tailscale_login FROM users")}


def test_forged_cookie_is_cleared_on_a_json_response(client, initialized_db, visitor):
    r = client.get(
        "/api/offline/snapshot",
        headers={"accept": "application/json", **cookie_header(FORGED)},
    )
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/json")
    raw = anon_set_cookie(r)
    assert raw is not None
    assert 'prep_anon=""' in raw or "prep_anon=;" in raw
    assert "Max-Age=0" in raw
    assert "Path=/" in raw


def test_octal_escaped_cookie_is_rejected_and_cleared(client, initialized_db, visitor):
    """A pure-ASCII header whose value un-quotes to a non-ASCII string
    is a dead cookie, not a 500: a raising verify would lock the
    browser out of every page with the cookie still set."""
    r = client.get(
        "/",
        headers={"cookie": f'{ac.COOKIE_NAME}="v1.q6urq6urq6urq6urq6urqw.1000.\\351AAAAAAAAAAA"'},
    )
    assert r.status_code == 200
    raw = anon_set_cookie(r)
    assert raw is not None and "Max-Age=0" in raw


def test_aging_cookie_is_refreshed_on_a_json_response(client, initialized_db, visitor):
    seed_anon_user()
    old = int(time.time()) - 31 * DAY
    r = client.get(
        "/api/offline/snapshot",
        headers=cookie_header(ac.mint_cookie(EXTERNAL_ID, issued_at=old)),
    )
    assert r.status_code == 200
    assert not r.headers["content-type"].startswith("text/html")
    raw = anon_set_cookie(r)
    assert raw is not None
    value = raw.split(";", 1)[0].split("=", 1)[1]
    refreshed = ac.verify_cookie(value)
    assert refreshed is not None
    assert refreshed.external_id == EXTERNAL_ID
    assert refreshed.issued_at > old
    assert "HttpOnly" in raw
    assert "SameSite=lax" in raw.replace("SameSite=Lax", "SameSite=lax")
    assert f"Max-Age={ac.MAX_AGE_SECONDS}" in raw
    # http in tests: the Secure attribute is omitted, not forced.
    assert "Secure" not in raw


def test_fresh_cookie_sets_no_cookie(client, initialized_db, visitor):
    seed_anon_user()
    r = client.get("/api/offline/snapshot", headers=cookie_header(ac.mint_cookie(EXTERNAL_ID)))
    assert r.status_code == 200
    assert anon_set_cookie(r) is None


def test_stale_cookie_is_cleared_on_html_too(client, initialized_db, visitor):
    r = client.get("/", headers=cookie_header(ac.mint_cookie(EXTERNAL_ID)))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    raw = anon_set_cookie(r)
    assert raw is not None and "Max-Age=0" in raw


def test_dormant_session_beats_a_valid_cookie_at_the_landing(client, initialized_db, dormant):
    seed_anon_user()
    r = client.get("/", headers=cookie_header(ac.mint_cookie(EXTERNAL_ID)))
    assert r.status_code == 200
    assert "Signing you in" in r.text
    assert anon_set_cookie(r) is None


def test_signed_in_beats_a_valid_cookie(client, initialized_db, signed_in):
    seed_anon_user()
    r = client.get("/", headers=cookie_header(ac.mint_cookie(EXTERNAL_ID)))
    assert r.status_code == 200
    assert "Signing you in" not in r.text
    ids = user_ids()
    assert "user_2abc" in ids
    # Provider identity wins, and the anonymous account it displaces is
    # merged into it: its row is gone and its cookie is cleared.
    assert EXTERNAL_ID not in ids
    raw = anon_set_cookie(r)
    assert raw is not None and "Max-Age=0" in raw


def test_a_page_view_mints_no_user(client, initialized_db, visitor):
    before = user_ids()
    for path in ("/", "/healthz", "/manifest.json", "/privacy"):
        assert client.get(path).status_code in (200, 307, 308)
    assert user_ids() == before
    assert not any(uid.startswith("anon:") for uid in user_ids())
