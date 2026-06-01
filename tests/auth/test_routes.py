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


# ---- /settings/srs — FSRS retention -------------------------------------


def test_srs_settings_renders_with_default(client: TestClient, initialized_db: str):
    """Fresh user — no row in users.desired_retention — gets the 0.90
    default highlighted as active, with the is_default flag set."""
    r = client.get("/settings/srs")
    assert r.status_code == 200
    assert "Desired retention" in r.text
    assert "90% — Default" in r.text


def test_srs_settings_save_persists(client: TestClient, initialized_db: str):
    """Saving a value writes it to users.desired_retention and the
    next GET shows it as the active preset."""
    r = client.post("/settings/srs", data={"retention": "0.85"})
    assert r.status_code == 200
    val = UserRepo().get_desired_retention(initialized_db)
    assert val == 0.85


def test_srs_settings_rejects_out_of_range(client: TestClient, initialized_db: str):
    r = client.post("/settings/srs", data={"retention": "0.50"})
    assert r.status_code == 400
    r2 = client.post("/settings/srs", data={"retention": "1.50"})
    assert r2.status_code == 400


def test_record_review_uses_user_retention(initialized_db: str):
    """End-to-end: setting users.desired_retention changes the
    interval the scheduler picks for the same card + verdict.
    A higher retention target → shorter interval (more frequent reviews).
    """
    from prep.auth.repo import UserRepo
    from prep.decks.entities import NewQuestion, QuestionType
    from prep.decks.repo import DeckRepo, QuestionRepo
    from prep.study.repo import ReviewRepo

    repo = UserRepo()
    deck_id = DeckRepo().get_or_create(initialized_db, "ret-test")
    qid = QuestionRepo().add(
        initialized_db,
        deck_id,
        NewQuestion(type=QuestionType.SHORT, prompt="Q", answer="A"),
    )
    # First review at default retention (0.90 baseline).
    repo.set_desired_retention(initialized_db, 0.80)
    state_low = ReviewRepo().record(initialized_db, qid, "right", user_answer="A")
    low_interval = state_low.interval_minutes

    # Second card, second user, different retention.
    repo.set_desired_retention(initialized_db, 0.95)
    qid2 = QuestionRepo().add(
        initialized_db,
        deck_id,
        NewQuestion(type=QuestionType.SHORT, prompt="Q2", answer="A2"),
    )
    state_high = ReviewRepo().record(initialized_db, qid2, "right", user_answer="A2")
    high_interval = state_high.interval_minutes

    # Higher retention → tighter scheduling → shorter interval.
    assert high_interval <= low_interval, (
        f"high-retention ({high_interval}m) should be ≤ " f"low-retention ({low_interval}m)"
    )
