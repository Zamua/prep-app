"""`signed_in_user`: which routes refuse an anonymous account, which
routes must keep admitting one, and the "Forget this device" exit.

The gated list is a table so every row of the spec's capability-gate
table is asserted, and a route that grows or loses a gate fails here.
"""

from __future__ import annotations

import pytest

from prep.auth import anon_cookie as ac
from prep.infrastructure.db import cursor
from tests.anon_support import ANON_ID

# (method, path, json_body). A JSON body marks the routes whose
# refusal is 403 rather than a redirect.
GATED = [
    ("GET", "/notify/log", None),
    ("GET", "/notify", None),
    ("POST", "/notify/prefs", {}),
    (
        "POST",
        "/notify/subscribe",
        {"endpoint": "https://push.example/x", "keys": {"p256dh": "k", "auth": "a"}},
    ),
    ("POST", "/notify/unsubscribe", {"endpoint": "https://push.example/x"}),
    ("POST", "/notify/test", {}),
    ("GET", "/settings/agent", None),
    ("POST", "/settings/agent/connect", None),
    ("POST", "/settings/agent/disconnect", None),
    ("POST", "/settings/agent/byok/anthropic-api/connect", None),
    ("POST", "/settings/agent/byok/anthropic-api/disconnect", None),
    ("POST", "/settings/agent/byok/anthropic-api/use", None),
    ("GET", "/settings/api", None),
    ("POST", "/settings/api/tokens", None),
    ("POST", "/settings/api/tokens/1/delete", None),
    ("GET", "/settings/account", None),
    ("POST", "/settings/account/delete", None),
    ("GET", "/decks/import-csv", None),
    ("POST", "/decks/import-csv", None),
    ("GET", "/decks/import-prepdeck", None),
    ("POST", "/decks/import-prepdeck", None),
    ("GET", "/decks/import-anki", None),
    ("POST", "/decks/import-anki", None),
]

UNGATED = [
    ("GET", "/settings/editor"),
    ("GET", "/settings/srs"),
]


def _call(c, method: str, path: str, body):
    if body is None:
        return c.request(method, path, follow_redirects=False)
    return c.request(method, path, json=body, follow_redirects=False)


@pytest.mark.parametrize(("method", "path", "body"), GATED, ids=[f"{m} {p}" for m, p, _ in GATED])
def test_gated_route_refuses_an_anonymous_account(anon_client, method, path, body):
    r = _call(anon_client, method, path, body)
    if body is not None:
        assert r.status_code == 403, r.text
        assert r.json()["detail"] == "sign in required"
    else:
        assert r.status_code == 303, r.text
        assert r.headers["location"].endswith("/sign-in")


@pytest.mark.parametrize(("method", "path", "body"), GATED, ids=[f"{m} {p}" for m, p, _ in GATED])
def test_gated_route_admits_a_signed_in_user(signed_in_client, method, path, body):
    """Past the gate. A signed-in caller may still get a 4xx from the
    route's own validation; what must not happen is the anonymous
    refusal shape."""
    r = _call(signed_in_client, method, path, body)
    assert r.status_code != 403
    assert not (r.status_code == 303 and r.headers.get("location", "").endswith("/sign-in"))


@pytest.mark.parametrize(
    "path",
    [
        "/notify/log",
        "/notify",
        "/settings/agent",
        "/settings/api",
        "/decks/import-csv",
        "/decks/import-prepdeck",
        "/decks/import-anki",
    ],
)
def test_gated_page_renders_for_a_signed_in_user(signed_in_client, path):
    """The refusal above is anonymity, not a route that is broken for
    everyone."""
    assert signed_in_client.get(path).status_code == 200


@pytest.mark.parametrize(("method", "path"), UNGATED, ids=[f"{m} {p}" for m, p in UNGATED])
def test_preference_route_admits_an_anonymous_account(anon_client, method, path):
    """Per-user preferences with nothing to protect stay on
    current_user; the merge carries both onto the target."""
    r = anon_client.request(method, path, follow_redirects=False)
    assert r.status_code == 200


def test_vapid_public_key_takes_no_user_at_all(anon_visitor):
    """The service worker fetches this where identity is not resolved;
    gating it would break subscribing for everyone."""
    r = anon_visitor.get("/notify/vapid-public-key")
    assert r.status_code == 200
    assert r.json()["key"]


def test_gated_route_still_401s_a_signed_out_visitor(anon_visitor):
    """The anonymous refusal is a new branch, not a replacement for
    the unauthenticated one."""
    r = anon_visitor.get("/settings/api", follow_redirects=False)
    assert r.status_code in (303, 401)
    if r.status_code == 303:
        assert r.headers["location"] == "/sign-in-here"


def test_forget_this_device_clears_the_cookie_and_keeps_the_account(anon_client):
    from prep.decks.repo import DeckRepo

    deck_id = DeckRepo().create(ANON_ID, "kept-deck")
    r = anon_client.post("/forget-device", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].endswith("/")
    cleared = [h for h in r.headers.get_list("set-cookie") if h.startswith(ac.COOKIE_NAME)]
    assert cleared and "Max-Age=0" in cleared[0]
    with cursor() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) AS n FROM users WHERE tailscale_login = ?", (ANON_ID,)
            ).fetchone()["n"]
            == 1
        )
        assert (
            c.execute("SELECT COUNT(*) AS n FROM decks WHERE id = ?", (deck_id,)).fetchone()["n"]
            == 1
        )


def test_a_cross_site_post_cannot_forget_the_device(anon_client):
    """The cookie is the account's only credential, so a page on
    another origin must not be able to clear it. SameSite=Lax stops
    the cookie riding the request; it does not stop the response's
    delete from applying."""
    r = anon_client.post(
        "/forget-device",
        headers={"sec-fetch-site": "cross-site"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert not [h for h in r.headers.get_list("set-cookie") if h.startswith(ac.COOKIE_NAME)]


def test_sign_out_drops_the_anonymous_cookie(signed_in_client):
    """Provider sign-out leaves `prep_anon` alone, so without this the
    browser resolves straight back into the pre-signup anonymous
    account: on a shared machine, the next person's dashboard."""
    r = signed_in_client.get("/sign-out", follow_redirects=False)
    assert r.status_code in (200, 303)
    cleared = [h for h in r.headers.get_list("set-cookie") if h.startswith(ac.COOKIE_NAME)]
    assert cleared and "Max-Age=0" in cleared[0]


def test_a_cross_site_sign_out_drops_no_cookie(signed_in_client):
    r = signed_in_client.get(
        "/sign-out",
        headers={"sec-fetch-site": "cross-site"},
        follow_redirects=False,
    )
    assert not [h for h in r.headers.get_list("set-cookie") if h.startswith(ac.COOKIE_NAME)]
