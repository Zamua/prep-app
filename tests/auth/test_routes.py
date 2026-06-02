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


def test_sign_out_renders_interstitial_on_clerk(client: TestClient, initialized_db: str):
    """Clerk sign-out needs ClerkJS to revoke the session — Clerk's
    hosted /sign-out URL isn't a real navigable page on their
    current account-portal config. So we render an interstitial
    that invokes Clerk.signOut() from JS and redirects home."""
    from prep.auth.providers import set_provider
    from prep.auth.providers.fake import FakeProvider

    class _PretendClerk(FakeProvider):
        name = "clerk"

    try:
        set_provider(_PretendClerk())
        r = client.get("/sign-out", follow_redirects=False)
        assert r.status_code == 200
        # Interstitial renders the page chrome + ClerkJS call.
        assert "Signing out" in r.text
        assert "Clerk.signOut" in r.text
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


# ---- per-deck retention override ----------------------------------------


def test_deck_retention_override_beats_user_default(initialized_db: str):
    """A deck-level override takes precedence over the user-level
    default. Set user to 0.80 (loose) but the deck to 0.95 (strict)
    — the deck's review should use 0.95, producing a tighter interval
    than the same setup without the override."""
    from prep.auth.repo import UserRepo
    from prep.decks.entities import NewQuestion, QuestionType
    from prep.decks.repo import DeckRepo, QuestionRepo
    from prep.study.repo import ReviewRepo

    UserRepo().set_desired_retention(initialized_db, 0.80)
    deck_repo = DeckRepo()
    deck_a = deck_repo.get_or_create(initialized_db, "loose-deck")
    deck_b = deck_repo.get_or_create(initialized_db, "strict-deck")
    deck_repo.set_desired_retention(initialized_db, deck_b, 0.95)

    qid_a = QuestionRepo().add(
        initialized_db, deck_a, NewQuestion(type=QuestionType.SHORT, prompt="Qa", answer="Aa")
    )
    qid_b = QuestionRepo().add(
        initialized_db, deck_b, NewQuestion(type=QuestionType.SHORT, prompt="Qb", answer="Ab")
    )
    state_a = ReviewRepo().record(initialized_db, qid_a, "right", user_answer="Aa")
    state_b = ReviewRepo().record(initialized_db, qid_b, "right", user_answer="Ab")

    assert state_b.interval_minutes <= state_a.interval_minutes, (
        f"strict deck override (95%) should schedule tighter than "
        f"the user's default (80%) — got deck={state_b.interval_minutes}m, "
        f"user-default={state_a.interval_minutes}m"
    )


def test_deck_retention_clear_falls_back_to_user(initialized_db: str):
    """Clearing a deck override (set to None) restores the user-level
    default's effect."""
    from prep.auth.repo import UserRepo
    from prep.decks.entities import NewQuestion, QuestionType
    from prep.decks.repo import DeckRepo, QuestionRepo
    from prep.study.repo import ReviewRepo

    UserRepo().set_desired_retention(initialized_db, 0.80)
    deck_repo = DeckRepo()
    deck_id = deck_repo.get_or_create(initialized_db, "cleared")

    # Deck override at 0.95 first.
    deck_repo.set_desired_retention(initialized_db, deck_id, 0.95)
    qid1 = QuestionRepo().add(
        initialized_db, deck_id, NewQuestion(type=QuestionType.SHORT, prompt="Q1", answer="A1")
    )
    state_override = ReviewRepo().record(initialized_db, qid1, "right", user_answer="A1")

    # Clear override.
    deck_repo.set_desired_retention(initialized_db, deck_id, None)
    qid2 = QuestionRepo().add(
        initialized_db, deck_id, NewQuestion(type=QuestionType.SHORT, prompt="Q2", answer="A2")
    )
    state_user = ReviewRepo().record(initialized_db, qid2, "right", user_answer="A2")

    # User default (0.80) is looser than 0.95 → longer interval after clear.
    assert state_user.interval_minutes >= state_override.interval_minutes


def test_deck_retention_route_saves_and_clears(client: TestClient, initialized_db: str):
    """POST /deck/<name>/retention with a float value stores the
    override; POST with 'default' clears it."""
    from prep.decks.repo import DeckRepo

    deck_repo = DeckRepo()
    deck_repo.get_or_create(initialized_db, "ret-route-test")

    # Save override.
    r = client.post(
        "/deck/ret-route-test/retention",
        data={"retention": "0.85"},
        follow_redirects=False,
    )
    assert r.status_code in (200, 303)
    deck_id = deck_repo.find_id(initialized_db, "ret-route-test")
    assert deck_repo.get_desired_retention(initialized_db, deck_id) == 0.85

    # Clear via the "default" sentinel.
    r2 = client.post(
        "/deck/ret-route-test/retention",
        data={"retention": "default"},
        follow_redirects=False,
    )
    assert r2.status_code in (200, 303)
    assert deck_repo.get_desired_retention(initialized_db, deck_id) is None


def test_deck_retention_route_rejects_trivia_deck(client: TestClient, initialized_db: str):
    """Retention is FSRS-only; trivia decks should 400."""
    from prep.decks.repo import DeckRepo

    DeckRepo().create_trivia(
        initialized_db, "trivia-no-retention", topic="topic", interval_minutes=60
    )
    r = client.post(
        "/deck/trivia-no-retention/retention",
        data={"retention": "0.85"},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_deck_retention_route_out_of_range(client: TestClient, initialized_db: str):
    from prep.decks.repo import DeckRepo

    DeckRepo().get_or_create(initialized_db, "ret-oor")
    r = client.post("/deck/ret-oor/retention", data={"retention": "0.50"})
    assert r.status_code == 400
