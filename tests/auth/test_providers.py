"""Unit tests for prep.auth.providers — provider selection +
resolve() behavior for the Tailscale, Fake, and Clerk adapters."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import Request

from prep.auth.port import AuthConfigError, ResolvedUser
from prep.auth.providers import _build_provider, get_provider, set_provider
from prep.auth.providers.fake import FakeProvider
from prep.auth.providers.tailscale import TailscaleProvider


def _make_request(headers: dict[str, str] | None = None) -> Request:
    """Build a minimal ASGI Request — enough for headers."""
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": raw_headers,
            "scheme": "https",
            "server": ("prepcards.app", 443),
            "client": ("127.0.0.1", 0),
        }
    )


# ---- registry ------------------------------------------------------


def test_get_provider_defaults_to_tailscale(monkeypatch):
    set_provider(None)  # clear cache
    monkeypatch.delenv("PREP_AUTH_MODE", raising=False)
    p = get_provider()
    assert isinstance(p, TailscaleProvider)
    set_provider(None)


def test_get_provider_respects_env(monkeypatch):
    set_provider(None)
    monkeypatch.setenv("PREP_AUTH_MODE", "fake")
    p = get_provider()
    assert isinstance(p, FakeProvider)
    set_provider(None)


def test_unknown_mode_raises(monkeypatch):
    set_provider(None)
    monkeypatch.setenv("PREP_AUTH_MODE", "garbage")
    with pytest.raises(AuthConfigError, match="unknown PREP_AUTH_MODE"):
        _build_provider()
    set_provider(None)


def test_set_provider_overrides_for_tests():
    sentinel = FakeProvider(external_id="pinned@example.com")
    set_provider(sentinel)
    assert get_provider() is sentinel
    set_provider(None)


# ---- TailscaleProvider --------------------------------------------


def test_tailscale_resolves_from_headers(monkeypatch):
    monkeypatch.delenv("PREP_DEFAULT_USER", raising=False)
    p = TailscaleProvider()
    req = _make_request(
        {
            "Tailscale-User-Login": "alice@example.com",
            "Tailscale-User-Name": "Alice",
            "Tailscale-User-Profile-Pic": "https://img/alice.png",
        }
    )
    user = p.resolve(req)
    assert user == ResolvedUser(
        external_id="alice@example.com",
        email="alice@example.com",
        display_name="Alice",
        profile_pic_url="https://img/alice.png",
        provider="tailscale",
    )


def test_tailscale_falls_back_to_default_user_env(monkeypatch):
    monkeypatch.setenv("PREP_DEFAULT_USER", "fallback@example.com")
    p = TailscaleProvider()
    user = p.resolve(_make_request())  # no headers
    assert user is not None
    assert user.external_id == "fallback@example.com"
    assert user.display_name == "fallback"  # local part of email


def test_tailscale_returns_none_when_unauthenticated(monkeypatch):
    monkeypatch.delenv("PREP_DEFAULT_USER", raising=False)
    p = TailscaleProvider()
    assert p.resolve(_make_request()) is None


def test_tailscale_urls_are_all_none():
    """Tailscale has no in-app sign-in/out flow — auth happens at
    the proxy. Templates use None to hide those controls."""
    urls = TailscaleProvider().urls()
    assert urls.sign_in is None
    assert urls.sign_out is None
    assert urls.account is None


# ---- FakeProvider --------------------------------------------------


def test_fake_returns_pinned_user():
    p = FakeProvider(
        external_id="user_test_123",
        email="test@example.com",
        display_name="Test User",
    )
    user = p.resolve(_make_request())
    assert user is not None
    assert user.external_id == "user_test_123"
    assert user.email == "test@example.com"
    assert user.provider == "fake"


def test_fake_can_be_unauthenticated():
    p = FakeProvider(signed_in=False)
    assert p.resolve(_make_request()) is None


# ---- ClerkProvider (mocked SDK) -----------------------------------


def test_clerk_provider_returns_user_from_jwt_payload(monkeypatch):
    """ClerkProvider hands FastAPI's request headers to the SDK;
    when SDK returns a signed-in RequestState, we map it to a
    ResolvedUser with external_id=sub."""
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_fake")
    monkeypatch.setenv("CLERK_AUTHORIZED_PARTIES", "https://prepcards.app")
    monkeypatch.setenv("CLERK_FRONTEND_API_URL", "https://accounts.prepcards.app")
    from prep.auth.providers.clerk import ClerkProvider

    p = ClerkProvider()
    state = MagicMock()
    state.is_signed_in = True
    state.payload = {
        "sub": "user_2abc",
        "email": "alice@example.com",
        "name": "Alice",
        "picture": "https://img/alice.png",
    }
    p._sdk.authenticate_request = MagicMock(return_value=state)
    user = p.resolve(_make_request({"cookie": "__session=fake"}))
    assert user is not None
    assert user.external_id == "user_2abc"
    assert user.email == "alice@example.com"
    assert user.display_name == "Alice"
    assert user.provider == "clerk"


def test_clerk_provider_returns_none_when_not_signed_in(monkeypatch):
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_fake")
    monkeypatch.setenv("CLERK_AUTHORIZED_PARTIES", "https://prepcards.app")
    monkeypatch.setenv("CLERK_FRONTEND_API_URL", "https://accounts.prepcards.app")
    from prep.auth.providers.clerk import ClerkProvider

    p = ClerkProvider()
    state = MagicMock()
    state.is_signed_in = False
    p._sdk.authenticate_request = MagicMock(return_value=state)
    assert p.resolve(_make_request()) is None


def test_clerk_provider_returns_none_on_sdk_exception(monkeypatch):
    """SDK errors (network, bad JWT) surface as unauthenticated, not
    as exceptions — keeps the route layer's 401 path consistent."""
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_fake")
    monkeypatch.setenv("CLERK_AUTHORIZED_PARTIES", "https://prepcards.app")
    monkeypatch.setenv("CLERK_FRONTEND_API_URL", "https://accounts.prepcards.app")
    from prep.auth.providers.clerk import ClerkProvider

    p = ClerkProvider()
    p._sdk.authenticate_request = MagicMock(side_effect=RuntimeError("boom"))
    assert p.resolve(_make_request()) is None


def test_clerk_provider_raises_when_env_missing(monkeypatch):
    monkeypatch.delenv("CLERK_SECRET_KEY", raising=False)
    monkeypatch.delenv("CLERK_AUTHORIZED_PARTIES", raising=False)
    monkeypatch.delenv("CLERK_FRONTEND_API_URL", raising=False)
    from prep.auth.providers.clerk import ClerkProvider

    with pytest.raises(AuthConfigError, match="CLERK_SECRET_KEY"):
        ClerkProvider()


def test_clerk_urls_include_redirect(monkeypatch):
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_fake")
    monkeypatch.setenv("CLERK_AUTHORIZED_PARTIES", "https://prepcards.app")
    monkeypatch.setenv("CLERK_FRONTEND_API_URL", "https://accounts.prepcards.app")
    from prep.auth.providers.clerk import ClerkProvider

    urls = ClerkProvider().urls()
    assert urls.sign_in.startswith("https://accounts.prepcards.app/sign-in")
    assert "redirect_url=" in urls.sign_in
    assert urls.sign_out.startswith("https://accounts.prepcards.app/sign-out")
    assert urls.account == "https://accounts.prepcards.app/user"


# ---- has_dormant_session (the PWA cold-launch handshake state) -----


def test_clerk_dormant_session_from_client_uat(monkeypatch):
    """Non-zero __client_uat with no valid JWT = Clerk's handshake
    state: the browser holds a live client session the server can't
    verify. That's "dormant", not "signed out"."""
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_fake")
    monkeypatch.setenv("CLERK_AUTHORIZED_PARTIES", "https://prepcards.app")
    monkeypatch.setenv("CLERK_FRONTEND_API_URL", "https://accounts.prepcards.app")
    from prep.auth.providers.clerk import ClerkProvider

    p = ClerkProvider()
    assert p.has_dormant_session(_make_request({"cookie": "__client_uat=1752768000"}))
    # "0" is Clerk's explicit signed-out marker; absence means the
    # browser never had a session. Neither is dormant.
    assert not p.has_dormant_session(_make_request({"cookie": "__client_uat=0"}))
    assert not p.has_dormant_session(_make_request())


def test_tailscale_never_reports_dormant_session():
    p = TailscaleProvider()
    assert not p.has_dormant_session(_make_request({"cookie": "__client_uat=1752768000"}))
