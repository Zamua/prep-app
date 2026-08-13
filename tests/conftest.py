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


@pytest.fixture(autouse=True)
def _no_claude_token_leak():
    """Autouse, defense-in-depth: ensure no test ends with a leaked
    CLAUDE_CODE_OAUTH_TOKEN in os.environ.

    Multiple code paths write the env var directly (production code
    in /settings/agent/connect, init_availability's load_into_env,
    etc.). monkeypatch can't auto-revert those because it didn't make
    the mutation. Without this fixture, the first test to set the env
    leaves a `sk-ant-oat01-fake-…` value behind, and every later test
    that exercises a `run_prompt` path will hand it to the real SDK,
    which then hangs 3+ min retrying the invalid token against
    Anthropic. Caught the hard way: a single leak ballooned the full
    suite from ~14s to 20+ min."""
    yield
    os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)


# ---- anonymous accounts -------------------------------------------------
#
# The three audiences the anonymous-cookie deploy serves. Each installs
# the real AnonymousFallbackProvider over a stub adapter, so identity
# resolves through the production precedence rule rather than a
# shortcut.


@pytest.fixture
def anon_secret(monkeypatch: pytest.MonkeyPatch):
    """A deploy that can sign cookies, which is what enables anonymous
    accounts at all."""
    from prep.auth import anon_cookie as ac

    monkeypatch.delenv(ac.SECRET_ENV, raising=False)
    monkeypatch.setenv(ac.MASTER_ENV, "11" * 32)


@pytest.fixture
def anon_visitor(client, initialized_db, anon_secret):
    """No provider identity and no cookie."""
    from prep.auth.providers import set_provider
    from prep.auth.providers.anon import AnonymousFallbackProvider
    from tests.anon_support import Inner

    set_provider(AnonymousFallbackProvider(Inner()))
    yield client
    set_provider(None)


@pytest.fixture
def anon_client(client, initialized_db, anon_secret):
    """An anonymous account, with its cookie on the client."""
    from prep.auth import anon_cookie as ac
    from prep.auth.providers import set_provider
    from prep.auth.providers.anon import AnonymousFallbackProvider
    from tests.anon_support import ANON_ID, Inner, seed_anon_user

    set_provider(AnonymousFallbackProvider(Inner()))
    seed_anon_user()
    client.cookies.set(ac.COOKIE_NAME, ac.mint_cookie(ANON_ID))
    yield client
    client.cookies.clear()
    set_provider(None)


@pytest.fixture
def signed_in_client(client, initialized_db, anon_secret):
    """A provider identity, plus the anonymous cookie the browser may
    still carry: the provider result must win."""
    from prep.auth import anon_cookie as ac
    from prep.auth.providers import set_provider
    from prep.auth.providers.anon import AnonymousFallbackProvider
    from tests.anon_support import ANON_ID, SIGNED_IN, Inner, seed_anon_user

    set_provider(AnonymousFallbackProvider(Inner(user=SIGNED_IN)))
    seed_anon_user()
    client.cookies.set(ac.COOKIE_NAME, ac.mint_cookie(ANON_ID))
    yield client
    client.cookies.clear()
    set_provider(None)


@pytest.fixture
def rendered_templates(monkeypatch: pytest.MonkeyPatch):
    """Records the template name of every render, so a test can assert
    two audiences reached the same one."""
    from prep.web.templates import templates

    names: list[str] = []
    original = templates.TemplateResponse

    def spy(*args, **kwargs):
        for arg in args:
            if isinstance(arg, str):
                names.append(arg)
                break
        return original(*args, **kwargs)

    monkeypatch.setattr(templates, "TemplateResponse", spy)
    return names
