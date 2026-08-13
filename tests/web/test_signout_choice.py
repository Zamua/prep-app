"""The masthead's sign-out row: where it renders, and the hook that
turns it into a choice.

Signing out leaves this device's offline snapshot in place, so on a
shared browser the next person can open the previous account's decks.
`data-signout-guard` is what lets app.js say so before it navigates
(the dialog and its three exits are asserted in
tests/e2e/test_device_wipe_e2e.py, which needs a browser). This file
pins the two server-side halves the browser suite cannot reach: that
the shipped markup carries the hook, and that the row itself follows
the provider rather than a hard-coded auth mode.
"""

from __future__ import annotations

import re

from prep.auth.port import SignInUrls
from prep.auth.providers import get_provider
from prep.decks.repo import DeckRepo
from tests.anon_support import ANON_ID

# The attribute app.js delegates on. Named here so a rename has to
# touch this pin.
HOOK = "data-signout-guard"


def signout_row(html: str) -> str:
    """The chip panel's sign-out list item, or "" when absent."""
    match = re.search(r'<ul class="user-links user-links-signout">.*?</ul>', html, re.S)
    return match.group(0) if match else ""


def test_the_sign_out_link_carries_the_guard_hook(signed_in_client):
    row = signout_row(signed_in_client.get("/").text)
    assert "Sign out" in row
    assert "/sign-out" in row
    assert HOOK in row


def test_no_sign_out_row_where_the_provider_has_none(signed_in_client, monkeypatch):
    """Tailscale-mode identity comes from the proxy and exposes no
    sign-out URL, so the row is absent rather than linking at a route
    that 404s."""
    provider = get_provider()
    monkeypatch.setattr(
        provider._inner, "urls", lambda: SignInUrls(sign_in=None, sign_out=None, account=None)
    )
    body = signed_in_client.get("/").text
    assert signout_row(body) == ""
    assert HOOK not in body


def test_an_anonymous_account_gets_the_forget_form_and_no_sign_out_guard(anon_client):
    """An anonymous account has no session to revoke: its exit is
    "Forget this device", which carries its own wipe hook."""
    DeckRepo().create(ANON_ID, "cookie-deck")
    body = anon_client.get("/").text
    assert "Forget this device" in body
    assert "data-forget-device" in body
    assert HOOK not in body
