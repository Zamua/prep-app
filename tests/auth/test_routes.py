"""HTTP route tests for the auth bounded context.

The auth context's only HTTP surface today is `/settings/editor` —
view + save the user's CodeMirror keybinding preference. Tests exercise
both the GET render and the POST persistence path through the
TestClient end-to-end.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from prep.auth.repo import UserRepo


def test_editor_settings_get_renders(client: TestClient, initialized_db: str):
    """GET shows the form pre-filled with the current mode (default
    `vanilla` for a brand-new user)."""
    r = client.get("/settings/editor")
    assert r.status_code == 200
    # Form submits back to the same path.
    assert "/settings/editor" in r.text
    # Default mode is reflected in the rendered chrome.
    assert "vanilla" in r.text


def test_editor_settings_post_persists_mode(client: TestClient, initialized_db: str):
    """POST a valid mode → row updated, GET returns the new mode."""
    r = client.post("/settings/editor", data={"mode": "vim"})
    assert r.status_code == 200
    # Round-trip: repo reflects the saved mode.
    assert UserRepo().get_editor_input_mode(initialized_db) == "vim"


def test_editor_settings_post_unknown_mode_400(client: TestClient, initialized_db: str):
    """Unknown mode is rejected before touching the DB — defends
    against a tampered form value."""
    # Seed a known mode so we can verify it didn't change.
    UserRepo().set_editor_input_mode(initialized_db, "vim")
    r = client.post("/settings/editor", data={"mode": "neovim-but-fancier"})
    assert r.status_code == 400
    assert UserRepo().get_editor_input_mode(initialized_db) == "vim"


# ---- /sign-in + /sign-out -----------------------------------------


def test_sign_in_404s_under_tailscale_provider(client: TestClient, initialized_db: str):
    """Tailscale auth has no in-app sign-in URL — /sign-in returns
    404 to make that explicit. (Templates check the provider's URLs
    and hide the Sign-in chip in that case.)"""
    r = client.get("/sign-in", follow_redirects=False)
    assert r.status_code == 404


def test_sign_out_404s_under_tailscale_provider(client: TestClient, initialized_db: str):
    r = client.get("/sign-out", follow_redirects=False)
    assert r.status_code == 404


def test_sign_in_redirects_when_provider_has_url(client: TestClient, initialized_db: str):
    """Use FakeProvider to exercise the redirect path without
    standing up a real Clerk hosted UI."""
    from prep.auth.providers import set_provider
    from prep.auth.providers.fake import FakeProvider

    try:
        set_provider(FakeProvider())
        r = client.get("/sign-in", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/fake/sign-in"
        r2 = client.get("/sign-out", follow_redirects=False)
        assert r2.status_code == 303
        assert r2.headers["location"] == "/fake/sign-out"
    finally:
        set_provider(None)


# ---- /settings/account --------------------------------------------


def test_account_settings_404s_under_tailscale(client: TestClient, initialized_db: str):
    """Tailscale-mode has no in-app delete flow — identity comes from
    the proxy; deleting the local row would just be recreated on the
    next request. The page returns 404 so the menu link doesn't
    surface a half-broken flow."""
    r = client.get("/settings/account")
    assert r.status_code == 404
    r2 = client.post("/settings/account/delete", data={"confirm": "anything"})
    assert r2.status_code == 404


def test_account_settings_renders_under_clerk(client: TestClient, initialized_db: str):
    from prep.auth.providers import set_provider
    from prep.auth.providers.fake import FakeProvider

    # Reuse FakeProvider with a custom name override so the route's
    # `provider.name == "clerk"` gate trips. Cheaper than a real
    # Clerk-backed provider, same code path through the route.
    class _PretendClerk(FakeProvider):
        name = "clerk"
        secret_key = "sk_test_fake"  # type: ignore[assignment]

    try:
        set_provider(_PretendClerk())
        r = client.get("/settings/account")
        assert r.status_code == 200
        assert "Danger zone" in r.text
        assert "Delete my account" in r.text
    finally:
        set_provider(None)


def test_account_delete_requires_matching_confirm(client: TestClient, initialized_db: str):
    """Typo in the confirm field → 400 + form re-renders with error.
    Pin the safety check so a future refactor doesn't accidentally
    drop it."""
    from prep.auth.providers import set_provider
    from prep.auth.providers.fake import FakeProvider

    class _PretendClerk(FakeProvider):
        name = "clerk"
        secret_key = "sk_test_fake"  # type: ignore[assignment]

    try:
        set_provider(_PretendClerk())
        r = client.post(
            "/settings/account/delete",
            data={"confirm": "wrong-value"},
        )
        assert r.status_code == 400
        assert "doesn&#39;t match" in r.text or "doesn't match" in r.text
    finally:
        set_provider(None)
