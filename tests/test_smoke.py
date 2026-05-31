"""Characterization smoke tests.

These exist to pin down the current happy-path behavior of the app
BEFORE the DDD refactor begins. If a future refactor breaks any of
these assertions, that's the safety net catching a regression.

These should remain green throughout the refactor. They're broad on
purpose — they assert the SHAPE of the response, not internal layout
that's about to change.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_index_returns_200_for_default_user(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    assert "<html" in r.text.lower()


def test_index_renders_landing_when_no_identity(env: None, monkeypatch):
    """With PREP_DEFAULT_USER unset and no Tailscale headers, `/`
    renders the public marketing landing instead of 401-ing. The
    auth invariant moved to protected routes (see
    `test_protected_route_401s_when_no_identity` below) — the home
    URL has to work for first-time visitors so they see what prep
    is before signing in.
    """
    monkeypatch.delenv("PREP_DEFAULT_USER", raising=False)

    import importlib

    from prep.infrastructure import db as db_mod

    importlib.reload(db_mod)

    from prep import app as app_mod

    importlib.reload(app_mod)

    c = TestClient(app_mod.app)
    r = c.get("/")
    assert r.status_code == 200
    # Marketing copy lives in the landing template, not the dashboard.
    assert "standing library" in r.text.lower()


def test_protected_route_401s_when_no_identity(env: None, monkeypatch):
    """The fundamental auth invariant — non-public routes still refuse
    an identity-less request. /notify/ has always required
    `current_user` and is a stable witness for the rule."""
    monkeypatch.delenv("PREP_DEFAULT_USER", raising=False)

    import importlib

    from prep.infrastructure import db as db_mod

    importlib.reload(db_mod)

    from prep import app as app_mod

    importlib.reload(app_mod)

    c = TestClient(app_mod.app)
    r = c.get("/notify", follow_redirects=False)
    assert r.status_code == 401


def test_index_uses_tailscale_header_when_present(env: None, authed_headers):
    """Tailscale headers always win over PREP_DEFAULT_USER."""
    import importlib

    from prep.infrastructure import db as db_mod

    importlib.reload(db_mod)

    from prep import app as app_mod

    importlib.reload(app_mod)

    with TestClient(app_mod.app) as c:
        r = c.get("/", headers=authed_headers)
    assert r.status_code == 200


def test_static_assets_served(client: TestClient):
    r = client.get("/static/css/index.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]
    # JS module bootstrap should also be reachable.
    r = client.get("/static/js/app.js")
    assert r.status_code == 200


def test_html_responses_carry_no_cache_header(client: TestClient):
    """HTML must re-validate on every navigation, otherwise iOS PWA
    standalone serves the previous deploy's index HTML forever — and
    that cached HTML points at stale `?v=…` CSS/JS, so the installed
    app drifts off whatever the latest deploy actually shipped."""
    r = client.get("/")
    assert r.status_code == 200
    cc = r.headers.get("cache-control", "")
    assert "no-cache" in cc, f"expected no-cache on HTML, got: {cc!r}"


def test_manifest_is_no_cache(client: TestClient):
    """Same reasoning for the PWA manifest — if it gets cached the
    installed app keeps its prior scope/icons forever after a rename."""
    r = client.get("/manifest.json")
    assert r.status_code == 200
    cc = r.headers.get("cache-control", "")
    assert "no-cache" in cc, f"expected no-cache on manifest, got: {cc!r}"


def test_static_css_still_cacheable(client: TestClient):
    """Inverse of the HTML test: hashed/versioned assets MUST NOT get
    the HTML middleware's no-cache stamped on them, otherwise the
    cache-bust dance breaks down (every browser refetches every CSS
    file on every nav). Pin the boundary so future middleware changes
    don't silently regress it."""
    r = client.get("/static/css/index.css")
    assert r.status_code == 200
    cc = r.headers.get("cache-control", "")
    assert "no-cache" not in cc, f"static CSS unexpectedly no-cache: {cc!r}"


def test_healthcheck_or_root_responds_quickly(client: TestClient):
    """/healthz is the canonical liveness probe (no DB hit, no
    template render). The docker-compose healthcheck targets it; this
    test pins the contract."""
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.text == "ok"
