"""AnonymousFallbackProvider: the precedence rule, the re-mint flag,
delegation, and where the registry composes it."""

from __future__ import annotations

import pytest
from starlette.requests import Request

from prep.auth import anon_cookie as ac
from prep.auth.port import ResolvedUser, SignInUrls
from prep.auth.providers.anon import AnonymousFallbackProvider

DAY = 86400
EXTERNAL_ID = "anon:" + "ab" * 16
MASTER = "11" * 32


@pytest.fixture
def secret(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(ac.SECRET_ENV, raising=False)
    monkeypatch.setenv(ac.MASTER_ENV, MASTER)


class _Inner:
    """Configurable stand-in for the wrapped adapter."""

    name = "inner"

    def __init__(self, *, user: ResolvedUser | None = None, dormant: bool = False):
        self._user = user
        self._dormant = dormant
        self.secret_key = "inner-only-attribute"

    def resolve(self, request):
        return self._user

    def urls(self):
        return SignInUrls(sign_in="/in", sign_out="/out", account="/acct")

    def has_dormant_session(self, request):
        return self._dormant


def make_request(cookies: dict[str, str] | None = None, scheme: str = "http") -> Request:
    headers = []
    if cookies:
        raw = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers.append((b"cookie", raw.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "root_path": "",
            "query_string": b"",
            "scheme": scheme,
            "server": ("testserver", 80),
            "headers": headers,
            "state": {},
        }
    )


SIGNED_IN = ResolvedUser(
    external_id="user_2abc",
    email="a@example.com",
    display_name="A",
    profile_pic_url=None,
    provider="inner",
)


def test_signed_in_beats_a_valid_cookie(secret):
    provider = AnonymousFallbackProvider(_Inner(user=SIGNED_IN))
    request = make_request({ac.COOKIE_NAME: ac.mint_cookie(EXTERNAL_ID)})
    resolved = provider.resolve(request)
    assert resolved == SIGNED_IN
    assert resolved.is_anonymous is False


def test_dormant_session_beats_a_valid_cookie(secret):
    provider = AnonymousFallbackProvider(_Inner(dormant=True))
    request = make_request({ac.COOKIE_NAME: ac.mint_cookie(EXTERNAL_ID)})
    assert provider.resolve(request) is None
    # The cookie is untouched: the browser keeps it for the merge.
    assert getattr(request.state, "anon_cookie_stale", False) is False


def test_cookie_resolves_when_nobody_else_does(secret):
    provider = AnonymousFallbackProvider(_Inner())
    request = make_request({ac.COOKIE_NAME: ac.mint_cookie(EXTERNAL_ID)})
    resolved = provider.resolve(request)
    assert resolved is not None
    assert resolved.external_id == EXTERNAL_ID
    assert resolved.is_anonymous is True
    assert resolved.email is None
    assert resolved.display_name == "Guest"


def test_visitor_without_a_cookie_resolves_nobody(secret):
    provider = AnonymousFallbackProvider(_Inner())
    request = make_request()
    assert provider.resolve(request) is None
    assert getattr(request.state, "anon_cookie_stale", False) is False


def test_forged_cookie_resolves_none_and_marks_the_request_stale(secret):
    provider = AnonymousFallbackProvider(_Inner())
    request = make_request({ac.COOKIE_NAME: "v1.q6urq6urq6urq6urq6urqw.1000.deadbeefdeadbeefdead"})
    assert provider.resolve(request) is None
    assert request.state.anon_cookie_stale is True


def test_aging_cookie_is_reminted_with_the_same_id(secret):
    provider = AnonymousFallbackProvider(_Inner())
    import time

    old = int(time.time()) - 31 * DAY
    request = make_request({ac.COOKIE_NAME: ac.mint_cookie(EXTERNAL_ID, issued_at=old)})
    resolved = provider.resolve(request)
    assert resolved is not None and resolved.external_id == EXTERNAL_ID

    refreshed = ac.verify_cookie(request.state.anon_cookie_refresh)
    assert refreshed is not None
    assert refreshed.external_id == EXTERNAL_ID
    assert refreshed.issued_at > old


def test_fresh_cookie_is_not_reminted(secret):
    provider = AnonymousFallbackProvider(_Inner())
    import time

    request = make_request(
        {ac.COOKIE_NAME: ac.mint_cookie(EXTERNAL_ID, issued_at=int(time.time()) - 29 * DAY)}
    )
    assert provider.resolve(request) is not None
    assert getattr(request.state, "anon_cookie_refresh", None) is None


def test_delegates_urls_name_and_dormant_unchanged(secret):
    inner = _Inner(dormant=True)
    provider = AnonymousFallbackProvider(inner)
    assert provider.urls() == inner.urls()
    assert provider.name == "inner"
    assert provider.has_dormant_session(make_request()) is True
    # Provider-specific extras stay reachable through the decorator.
    assert provider.secret_key == "inner-only-attribute"
    missing = "no_such_attribute"
    with pytest.raises(AttributeError):
        getattr(provider, missing)


def test_registry_wraps_only_when_a_secret_resolves(monkeypatch: pytest.MonkeyPatch):
    from prep.auth import providers

    monkeypatch.setenv("PREP_AUTH_MODE", "fake")
    monkeypatch.delenv(ac.SECRET_ENV, raising=False)
    monkeypatch.delenv(ac.MASTER_ENV, raising=False)
    assert not isinstance(providers._build_provider(), AnonymousFallbackProvider)

    monkeypatch.setenv(ac.MASTER_ENV, MASTER)
    built = providers._build_provider()
    assert isinstance(built, AnonymousFallbackProvider)
    assert built.name == "fake"
