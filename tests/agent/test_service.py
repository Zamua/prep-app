"""Tests for the agent context's status probe — post-SDK migration.

The probe is now purely local: SDK importable + a Claude OAuth
token reachable (either in the env or in the prep-data token file).
No more HTTP probes to an agent-server; no more "is the bin
executable" check.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def isolated_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Reset all env vars + point PREP_DATA_DIR at a fresh tmp dir so
    each test sees a clean status environment."""
    monkeypatch.setenv("PREP_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    # Legacy vars — the new probe doesn't read them, but explicit
    # delenv makes the test intent clear.
    monkeypatch.delenv("PREP_AGENT_URL", raising=False)
    monkeypatch.delenv("PREP_AGENT_BIN", raising=False)
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    return tmp_path


def test_status_unconfigured_when_no_token_anywhere(isolated_status):
    """No env var set + no token file on disk → kind=unconfigured."""
    import importlib

    st = importlib.import_module("prep.agent.status")

    s = st.status()
    assert s["kind"] == "unconfigured"
    assert s["logged_in"] is False
    assert "reason" in s


def test_status_logged_in_when_env_var_set(isolated_status, monkeypatch: pytest.MonkeyPatch):
    """CLAUDE_CODE_OAUTH_TOKEN env var set → kind=sdk, logged_in=True."""
    import importlib

    st = importlib.import_module("prep.agent.status")

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-anything")
    s = st.status()
    assert s["kind"] == "sdk"
    assert s["logged_in"] is True


def test_status_logged_in_when_token_file_present(isolated_status):
    """Token file in PREP_DATA_DIR → kind=sdk, logged_in=True. Path the
    /settings/agent/connect handler writes to gets surfaced as truth."""
    import importlib

    st = importlib.import_module("prep.agent.status")
    from prep.agent import token_store

    token_store.write_token("sk-ant-oat01-from-disk")
    s = st.status()
    assert s["kind"] == "sdk"
    assert s["logged_in"] is True


def test_set_available_and_probe(monkeypatch: pytest.MonkeyPatch):
    """`set_available` is the only blessed way to mutate the cached
    flag templates read; `probe()` is the boolean view of `status()`."""
    import importlib

    st = importlib.import_module("prep.agent.status")

    st.set_available(True)
    assert st.is_available is True
    st.set_available(False)
    assert st.is_available is False

    # probe wraps status() with a bool view — works regardless of
    # what status() actually returns.
    monkeypatch.setattr(st, "status", lambda: {"logged_in": True})
    assert st.probe() is True
    monkeypatch.setattr(st, "status", lambda: {"logged_in": False})
    assert st.probe() is False


def test_init_availability_loads_token_from_file(isolated_status):
    """init_availability reads the token file into the env so a fresh
    container boot (where the env var isn't set, but the file is)
    activates the SDK without manual intervention."""
    import importlib
    import os

    st = importlib.import_module("prep.agent.status")
    from prep.agent import token_store

    token_store.write_token("sk-ant-oat01-boot-test")
    assert os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") is None

    st.init_availability()
    assert os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") == "sk-ant-oat01-boot-test"
    assert st.is_available is True
