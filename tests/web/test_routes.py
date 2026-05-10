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
