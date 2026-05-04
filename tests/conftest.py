"""Shared pytest fixtures for the prep test suite.

Goals:
- Each test gets a fresh, isolated SQLite DB (in-memory file under tmp).
- The FastAPI app is constructed against that DB via dependency overrides.
- Default user is a stable test identity so ownership-scoped accessors
  resolve without us threading auth through every test.

Importing this file is what triggers the side-effects — pytest auto-loads
conftest.py from each test dir's parent chain.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

# Set env vars BEFORE importing app/db modules — they read PREP_DB_PATH
# at import time. We use a per-test temp file (not :memory: because the
# app uses multiple connections and :memory: would isolate them).


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Per-test sqlite file. Cleaned up automatically with tmp_path."""
    return tmp_path / "test.sqlite"


@pytest.fixture
def env(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Configure env vars for the test app, scoped to the test."""
    monkeypatch.setenv("PREP_DB_PATH", str(db_path))
    monkeypatch.setenv("PREP_DEFAULT_USER", "testuser@example.com")
    monkeypatch.setenv("PREP_VAPID_KEYS_PATH", str(db_path.parent / "vapid-keys.json"))
    monkeypatch.setenv("PREP_VAPID_PEM_PATH", str(db_path.parent / "vapid-private.pem"))
    # The Temporal client is constructed at module import time when not
    # mocked; we don't need it for unit tests.
    monkeypatch.setenv("TEMPORAL_HOST_PORT", "127.0.0.1:0")
    yield


@pytest.fixture
def client(env: None):
    """A FastAPI TestClient against a fresh, isolated app instance.

    Uses importlib.reload so each test sees a fresh module state; otherwise
    the connection cache in prep.infrastructure.db carries state across
    tests. TestClient is entered as a context manager so the on_event
    "startup" handler (which runs db.init() to bootstrap the schema)
    actually fires — without it, smoke tests that don't use the
    initialized_db fixture would hit "no such table: users"."""
    import importlib

    from prep.infrastructure import db as db_mod

    importlib.reload(db_mod)

    from prep import app as app_mod

    importlib.reload(app_mod)

    from fastapi.testclient import TestClient

    with TestClient(app_mod.app) as c:
        yield c


@pytest.fixture
def authed_headers() -> dict[str, str]:
    """Tailscale-style identity headers for tests that want the explicit
    auth path rather than the PREP_DEFAULT_USER bypass."""
    return {
        "Tailscale-User-Login": "alice@example.com",
        "Tailscale-User-Name": "Alice",
    }


@pytest.fixture
def initialized_db(env: None):
    """Run db.init() against the per-test sqlite path so a repo test
    has tables to read/write. Returns the test user id (already
    upserted into users) to thread through repo calls.

    Uses whatever PREP_DEFAULT_USER `env` set so route tests (driven
    via TestClient → app's auth dependency) and repo tests (direct
    function calls) operate on the same user without an additional
    headers dance."""
    import importlib
    import os

    from prep.infrastructure import db as _infra_db

    importlib.reload(_infra_db)
    _infra_db.init()

    from prep.auth.repo import UserRepo

    user_id = os.environ["PREP_DEFAULT_USER"]
    UserRepo().upsert(user_id, display_name=user_id.split("@")[0])
    return user_id


# Force-disable noisy startup logs during tests.
os.environ.setdefault("PYTHONWARNINGS", "ignore")
