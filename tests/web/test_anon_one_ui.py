"""One UI: an anonymous account is served the same surfaces a
signed-in user is, and the only differences are the three exceptions
the design allows.

Exception 1 is the landing splash; exception 2 is the capability
gates (asserted in tests/auth/test_signed_in_user_gates.py); exception
3 is the chip panel's body, asserted here against rendered HTML so a
future panel entry pointing at a gated route fails.
"""

from __future__ import annotations

import re

from prep.auth import anon_cookie as ac
from prep.auth.providers import set_provider
from prep.auth.providers.anon import AnonymousFallbackProvider
from prep.decks.repo import DeckRepo
from tests.anon_support import ANON_ID, SIGNED_IN, Inner, seed_named_user

# Every route the capability-gate table gates. The chip panel must
# link to none of them.
GATED_HREFS = (
    "/notify/log",
    "/notify",
    "/settings/agent",
    "/settings/api",
    "/settings/account",
)


def hrefs(html: str) -> list[str]:
    return re.findall(r'href="([^"]*)"', html)


def panel(html: str) -> str:
    """The chip panel's markup only."""
    start = html.index('<div class="user-panel">')
    return html[start : html.index("</details>", start)]


# ---- exception 1: the landing splash -----------------------------------


def test_a_signed_out_visitor_gets_the_landing(anon_visitor, rendered_templates):
    r = anon_visitor.get("/")
    assert r.status_code == 200
    assert "landing.html" in rendered_templates


def test_an_anonymous_account_with_a_deck_gets_the_dashboard(anon_client, rendered_templates):
    DeckRepo().create(ANON_ID, "cookie-deck")
    r = anon_client.get("/")
    assert r.status_code == 200
    assert "index.html" in rendered_templates
    assert "landing.html" not in rendered_templates


def test_the_dashboard_is_the_same_template_for_both_audiences(anon_client, rendered_templates):
    """One template for both, chosen by neither."""
    DeckRepo().create(ANON_ID, "cookie-deck")
    assert anon_client.get("/").status_code == 200
    anon_template = rendered_templates[-1]

    seed_named_user()
    set_provider(AnonymousFallbackProvider(Inner(user=SIGNED_IN)))
    assert anon_client.get("/").status_code == 200
    assert rendered_templates[-1] == anon_template == "index.html"


def test_deck_page_and_study_shell_are_the_same_templates(anon_client, rendered_templates):
    DeckRepo().create(ANON_ID, "cookie-deck")
    assert anon_client.get("/deck/cookie-deck").status_code == 200
    assert anon_client.get("/study/cookie-deck").status_code == 200
    assert "deck.html" in rendered_templates
    assert "study_shell.html" in rendered_templates


def test_no_anonymous_only_markup_on_the_dashboard(anon_client):
    DeckRepo().create(ANON_ID, "cookie-deck")
    body = anon_client.get("/").text
    # The raw cookie id is the one identifier that must never render.
    assert ANON_ID not in body
    assert "anon:" not in body


# ---- exception 3: the chip panel ---------------------------------------


def test_the_chip_renders_for_an_anonymous_account(anon_client):
    body = anon_client.get("/").text
    assert 'class="user-indicator"' in body
    assert 'class="user-chip"' in body


def test_the_anonymous_panel_keeps_only_the_ungated_entries(anon_client):
    body = panel(anon_client.get("/").text)
    assert "Guest" in body
    assert "Not signed in" in body
    assert "/settings/srs" in body
    assert "/settings/editor" in body
    assert "Forget this device" in body
    assert "Create an account to keep your decks" in body
    assert "/sign-up-here" in body
    # The way back for someone who already has an account: the panel
    # must offer sign-IN too, not only account creation.
    assert "Already have an account? Sign in" in body
    assert "/sign-in-here" in body


def test_the_anonymous_panel_links_to_no_gated_route(anon_client):
    """Written against the rendered_templates HTML: a future entry pointing at a
    gated route fails here."""
    linked = hrefs(panel(anon_client.get("/").text))
    for gated in GATED_HREFS:
        assert not any(href.endswith(gated) for href in linked), gated
    assert "Activity" not in panel(anon_client.get("/").text)


def test_the_signed_in_panel_keeps_every_entry(signed_in_client):
    body = panel(signed_in_client.get("/").text)
    assert "/notify/log" in body
    assert "/settings/agent" in body
    assert "/settings/api" in body
    assert "/notify" in body
    assert "Forget this device" not in body
    assert "Create an account to keep your decks" not in body


def test_the_panel_degrades_where_there_is_no_sign_in_flow(anon_client, monkeypatch):
    """In tailscale mode the primary link renders nothing rather than
    a dead anchor."""
    from prep.auth.port import SignInUrls
    from prep.auth.providers import get_provider

    provider = get_provider()
    monkeypatch.setattr(
        provider._inner, "urls", lambda: SignInUrls(sign_in=None, sign_out=None, account=None)
    )
    body = panel(anon_client.get("/").text)
    assert "Create an account to keep your decks" not in body
    assert "Forget this device" in body
    assert "/settings/srs" in body


# ---- the install gates + the bootstrap flag ----------------------------


def test_neither_install_entry_renders_for_an_anonymous_account(anon_client):
    body = anon_client.get("/").text
    assert "pwa-install-pill" not in body
    assert "colophon-install" not in body


def test_both_install_entries_render_for_a_signed_in_user(signed_in_client):
    body = signed_in_client.get("/").text
    assert "pwa-install-pill" in body
    assert "colophon-install" in body


def test_the_clerk_bootstrap_flag_reads_signed_out_for_an_anonymous_account(
    anon_client, monkeypatch
):
    """Asserted against the rendered_templates attribute, not the expression: a
    Clerk user whose JWT expired on a browser holding prep_anon still
    needs the session recovery to run."""
    monkeypatch.setenv("PREP_AUTH_MODE", "clerk")
    monkeypatch.setenv("CLERK_PUBLISHABLE_KEY", "pk_test_" + _b64("clerk.example.com$"))
    body = anon_client.get("/").text
    assert "window.__prepServerSignedOut = true;" in body


def test_the_clerk_bootstrap_flag_reads_signed_in_for_a_provider_user(
    signed_in_client, monkeypatch
):
    monkeypatch.setenv("PREP_AUTH_MODE", "clerk")
    monkeypatch.setenv("CLERK_PUBLISHABLE_KEY", "pk_test_" + _b64("clerk.example.com$"))
    body = signed_in_client.get("/").text
    assert "window.__prepServerSignedOut = false;" in body


def _b64(value: str) -> str:
    import base64

    return base64.b64encode(value.encode()).decode().rstrip("=")


# ---- the cookie exit ----------------------------------------------------


def test_forget_this_device_returns_the_browser_to_the_landing(anon_client, rendered_templates):
    anon_client.post("/forget-device", follow_redirects=False)
    anon_client.cookies.delete(ac.COOKIE_NAME)
    assert anon_client.get("/").status_code == 200
    assert rendered_templates[-1] == "landing.html"
