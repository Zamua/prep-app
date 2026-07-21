"""Fixtures shared by the offline-context route tests.

The offline surfaces split on auth: /offline and /sw.js are
un-auth-gated on purpose (they must be reachable and cacheable with
no live session), while /api/offline/snapshot requires the standard
current_user dependency. The unauthed_client fixture gives the tests
an app instance with the PREP_DEFAULT_USER bypass removed so both
sides of that split can be asserted.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def unauthed_client(env: None, monkeypatch: pytest.MonkeyPatch):
    """TestClient against an app with NO identity available: the
    PREP_DEFAULT_USER bypass is unset and no Tailscale headers are
    sent. Same reload dance as tests/test_smoke.py so the fresh env
    is what the app instance sees."""
    monkeypatch.delenv("PREP_DEFAULT_USER", raising=False)

    import importlib

    from prep.infrastructure import db as db_mod

    importlib.reload(db_mod)

    from prep import app as app_mod

    importlib.reload(app_mod)

    from fastapi.testclient import TestClient

    with TestClient(app_mod.app) as c:
        yield c
