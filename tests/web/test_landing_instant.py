"""Landing hero swap gates (docs/INSTANT-START.md section 2): the
instant hero renders only when the deploy's free tier is configured
AND the provider exposes a sign-in URL. A deploy failing either gate
renders the marketing hero unchanged. Also pins the /offline shell's
sign_in_url data attribute (the guest-mode nudges read it)."""

from __future__ import annotations

import pytest

from prep.auth.port import SignInUrls
from prep.auth.providers import set_provider

INSTANT_HOOK = "data-instant-start"
INSTANT_HEADLINE = "What do you want to"
MARKETING_CTA = "Start a deck, it's free"
NOSCRIPT_COPY = "Instant decks need JavaScript. Sign in to build decks instead."
DISCLOSURE_COPY = "Deck generation runs on a shared free AI service"


class _VisitorProvider:
    """Resolves nobody; sign-in URL fixed at build time."""

    name = "stub"

    def __init__(self, sign_in: str | None = "/sign-in"):
        self._sign_in = sign_in

    def resolve(self, request):
        return None

    def urls(self):
        return SignInUrls(sign_in=self._sign_in, sign_out=None, account=None)

    def has_dormant_session(self, request):
        return False


@pytest.fixture
def visitor_provider():
    set_provider(_VisitorProvider())
    yield
    set_provider(None)


@pytest.fixture
def visitor_provider_no_sign_in():
    set_provider(_VisitorProvider(sign_in=None))
    yield
    set_provider(None)


@pytest.fixture
def free_tier(monkeypatch):
    monkeypatch.setenv("PREP_FREE_INFERENCE_BASE_URL", "https://inference.example.test/v1")
    monkeypatch.setenv("PREP_FREE_INFERENCE_API_KEY", "test-key")
    monkeypatch.setenv("PREP_FREE_INFERENCE_MODEL", "test-model")


@pytest.fixture
def no_free_tier(monkeypatch):
    monkeypatch.delenv("PREP_FREE_INFERENCE_BASE_URL", raising=False)
    monkeypatch.delenv("PREP_FREE_INFERENCE_API_KEY", raising=False)
    monkeypatch.delenv("PREP_FREE_INFERENCE_MODEL", raising=False)


def test_both_gates_pass_renders_instant_hero(client, initialized_db, visitor_provider, free_tier):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert INSTANT_HOOK in body
    assert INSTANT_HEADLINE in body
    assert "Generate my deck" in body
    assert NOSCRIPT_COPY in body
    assert DISCLOSURE_COPY in body
    # Marketing hero CTA is gone; the demoted sections stay below.
    assert MARKETING_CTA not in body
    assert "How it works" in body
    # Masthead sign-in chip stays.
    assert 'href="/sign-in"' in body


def test_no_free_tier_renders_marketing_hero(
    client, initialized_db, visitor_provider, no_free_tier
):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert INSTANT_HOOK not in body
    assert MARKETING_CTA in body


def test_no_sign_in_url_renders_marketing_hero(
    client, initialized_db, visitor_provider_no_sign_in, free_tier
):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert INSTANT_HOOK not in body
    # Old hero headline; the CTA row is hidden without a sign-in URL.
    assert "Remember" in body


def test_offline_shell_carries_sign_in_url(client, visitor_provider):
    r = client.get("/offline")
    assert r.status_code == 200
    assert 'data-sign-in-url="/sign-in"' in r.text


def test_offline_shell_sign_in_url_empty_without_provider_url(client, visitor_provider_no_sign_in):
    r = client.get("/offline")
    assert r.status_code == 200
    assert 'data-sign-in-url=""' in r.text


def test_topic_placeholder_rotates_from_the_pool(
    client, initialized_db, visitor_provider, free_tier, monkeypatch
):
    """The hero textarea's ghost text is one of TOPIC_PLACEHOLDERS,
    chosen per render: two renders with pinned choices produce the
    pinned placeholder, and an unpinned render always draws from the
    pool (never an empty or foreign string)."""
    import random as _random

    from prep.web import index as index_mod

    real_choice = _random.choice
    seen = set()
    for pick in (index_mod.TOPIC_PLACEHOLDERS[0], index_mod.TOPIC_PLACEHOLDERS[1]):
        monkeypatch.setattr(_random, "choice", lambda seq, _p=pick: _p)
        body = client.get("/").text
        assert f'placeholder="{pick}"' in body
        seen.add(pick)
    assert len(seen) == 2

    monkeypatch.setattr(_random, "choice", real_choice)
    body = client.get("/").text
    assert any(f'placeholder="{p}"' in body for p in index_mod.TOPIC_PLACEHOLDERS)


def test_install_nudge_hidden_for_anonymous_visitors(
    client, initialized_db, visitor_provider, free_tier
):
    """iOS gives an installed PWA storage separate from Safari, so a
    guest who installs opens an empty app while their local-only deck
    stays behind. The install surfaces render for signed-in users
    only."""
    body = client.get("/").text
    assert "pwa-install-root" not in body
    assert "colophon-install" not in body


def test_install_nudge_renders_for_signed_in_users(client, initialized_db, authed_headers):
    body = client.get("/", headers=authed_headers).text
    assert "pwa-install-root" in body
    assert "colophon-install" in body
