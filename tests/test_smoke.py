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


def test_index_401s_when_no_identity(env: None, monkeypatch):
    """With PREP_DEFAULT_USER unset and no Tailscale headers, the app
    must refuse the request. This is the fundamental auth invariant —
    it should hold across any refactor."""
    monkeypatch.delenv("PREP_DEFAULT_USER", raising=False)

    import importlib

    from prep.infrastructure import db as db_mod

    importlib.reload(db_mod)

    from prep import app as app_mod

    importlib.reload(app_mod)

    c = TestClient(app_mod.app)
    r = c.get("/")
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
    r = client.get("/static/style.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]


def test_healthcheck_or_root_responds_quickly(client: TestClient):
    """No /healthz endpoint exists today; we use / as a liveness probe.
    If a /healthz route is ever added, update this test (and consider
    making it the canonical liveness target)."""
    r = client.get("/")
    assert r.status_code == 200
