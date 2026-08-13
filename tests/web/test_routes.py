"""Route tests for the cross-cutting web layer.

Three surfaces live here:
- `/`             — the index/home page (decks + recent sessions)
- `/manifest.json` — PWA manifest (UN-AUTHED on purpose)
- `/sw.js`         — service worker (UN-AUTHED on purpose)

Tests run through TestClient against the per-test sqlite. The
auth-bypass uses the same PREP_DEFAULT_USER fixture every other
context relies on.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from prep.decks.repo import DeckRepo


def test_index_renders_for_user_with_no_decks(client: TestClient, initialized_db: str):
    """Brand-new user → index still 200s. No "you have no decks"
    branch is exercised here; we just want to know the empty-state
    path doesn't crash."""
    r = client.get("/")
    assert r.status_code == 200


def test_index_renders_with_pinned_and_unpinned_decks(client: TestClient, initialized_db: str):
    """Pinned decks float to the top of the list; unpinned ones go
    below. The route splits the repo's ordered list into two groups
    so the template can render them as separate sections."""
    deck_repo = DeckRepo()
    pinned_id = deck_repo.create(initialized_db, "pinned-deck")
    deck_repo.create(initialized_db, "alpha")
    deck_repo.set_pinned(initialized_db, pinned_id, True)

    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    # Both deck names show up.
    assert "pinned-deck" in body
    assert "alpha" in body
    # And the pinned one comes before the unpinned one in the rendered
    # HTML (group ordering, not strict z-index).
    assert body.index("pinned-deck") < body.index("alpha")


def test_dashboard_overview_endpoint_answers_the_source_contract(
    client: TestClient, initialized_db: str
):
    """The JSON the ServerSource adapter reads. Shape is the DeckSource
    contract in static/js/dashboard/source.js; `unsynced` is null
    because an outbox is a client-side store the server cannot see."""
    deck_repo = DeckRepo()
    pinned_id = deck_repo.create(initialized_db, "pinned-deck")
    deck_repo.create(initialized_db, "alpha")
    deck_repo.set_pinned(initialized_db, pinned_id, True)

    payload = client.get("/api/dashboard/overview").json()
    assert set(payload) == {"user", "decks", "due", "total", "nextDueMinutes", "unsynced"}
    assert payload["unsynced"] is None
    assert [d["slug"] for d in payload["decks"]] == ["pinned-deck", "alpha"]
    assert [d["pinned"] for d in payload["decks"]] == [True, False]
    assert set(payload["decks"][0]) == {
        "id",
        "slug",
        "display_name",
        "due",
        "total",
        "deck_type",
        "pinned",
        "trivia_stats",
    }
    assert set(payload["user"]) == {"display_name", "is_anonymous"}


def test_dashboard_shell_embeds_what_the_endpoint_answers(client: TestClient, initialized_db: str):
    """One payload builder feeds both, so the first paint and any
    later re-read cannot describe different decks."""
    DeckRepo().create(initialized_db, "shared-truth")
    body = client.get("/").text
    marker = '<script type="application/json" id="dashboard-overview">'
    embedded = body.split(marker, 1)[1].split("</script>", 1)[0]
    assert json.loads(embedded) == client.get("/api/dashboard/overview").json()


def test_embedded_overview_cannot_break_out_of_its_script_tag(
    client: TestClient, initialized_db: str
):
    """A deck label is user text embedded inside a <script> element,
    where the parser ends the element at the first literal `</script>`
    regardless of JSON quoting. The escape has to happen at the
    serializer, so the payload still decodes to the exact label."""
    label = '</script><img src=x onerror="alert(1)">'
    DeckRepo().create(initialized_db, "hostile", display_name=label)
    body = client.get("/").text
    assert "<img src=x" not in body
    marker = '<script type="application/json" id="dashboard-overview">'
    embedded = body.split(marker, 1)[1].split("</script>", 1)[0]
    assert json.loads(embedded)["decks"][0]["display_name"] == label


def test_dashboard_deck_menus_render_one_menu_per_deck(client: TestClient, initialized_db: str):
    """Every row of the overflow menu is a server route, so the server
    composes it and the host mounts it. Keyed by deck id: that is how
    the host pairs a menu with the row it belongs to."""
    deck_repo = DeckRepo()
    first = deck_repo.create(initialized_db, "alpha")
    second = deck_repo.create(initialized_db, "beta")
    r = client.get("/api/dashboard/deck-menus")
    assert r.status_code == 200
    assert f'data-deck-menu="{first}"' in r.text
    assert f'data-deck-menu="{second}"' in r.text
    assert "/deck/alpha/pin" in r.text
    assert "/deck/beta/export" in r.text


def test_manifest_unauthed_and_serves_root_path_aware_payload(client: TestClient, monkeypatch):
    """Manifest is intentionally un-auth-gated (the install handshake
    can't reliably carry Tailscale headers). Scope/start_url tracks
    ROOT_PATH so /prep and /prep-staging both install correctly."""
    monkeypatch.setenv("ROOT_PATH", "/prep")
    r = client.get("/manifest.json")
    assert r.status_code == 200
    payload = r.json()
    assert payload["scope"] == "/prep/"
    assert payload["start_url"] == "/prep/"
    # Icons honor ROOT_PATH so they resolve through the proxy.
    icons = payload["icons"]
    assert all(i["src"].startswith("/prep/") for i in icons)


def test_manifest_default_when_root_path_unset(client: TestClient, monkeypatch):
    """Without ROOT_PATH (the bare-host deploy case), scope falls back
    to '/' so the PWA installs correctly on hostnames without a
    sub-path mount."""
    monkeypatch.delenv("ROOT_PATH", raising=False)
    r = client.get("/manifest.json")
    assert r.status_code == 200
    payload = r.json()
    assert payload["scope"] == "/"
    assert payload["start_url"] == "/"


def test_service_worker_served_at_root(client: TestClient):
    """SW must be served at the app's root scope (not /static/sw.js)
    so its scope covers the whole app. Browser uses the SW's URL
    path as its scope, so this URL is what determines what it
    controls."""
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers.get("content-type", "")
