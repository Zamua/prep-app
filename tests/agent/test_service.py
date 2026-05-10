"""Tests for the agent context's service-shaped surface.

The agent context's "service" today is `prep.agent.status` — a probe
function that shells out to `PREP_AGENT_URL/healthz` (HTTP mode), an
executable check on `PREP_AGENT_BIN` (shell mode), or returns
"unconfigured" when neither is set. There's also a small cached
`is_available` flag that templates read.

We exercise each of the three branches by monkeypatching
`urllib.request.urlopen` (HTTP) or env vars + `os.path.isfile` /
`os.access` (shell) so the test runs in any environment.
"""

from __future__ import annotations

import json


def test_status_unconfigured_when_no_env_set(monkeypatch, tmp_path):
    """Neither PREP_AGENT_URL nor PREP_AGENT_BIN set, and the default
    bin path doesn't resolve → kind=unconfigured."""
    import importlib

    st = importlib.import_module("prep.agent.status")

    monkeypatch.delenv("PREP_AGENT_URL", raising=False)
    monkeypatch.delenv("PREP_AGENT_BIN", raising=False)
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    # _DEFAULT_BIN was bound at import time off $HOME; pin it to a path
    # that definitely doesn't exist so the fallback "executable?" check
    # fails deterministically.
    monkeypatch.setattr(st, "_DEFAULT_BIN", str(tmp_path / "no-such-claude"))
    s = st.status()
    assert s["kind"] == "unconfigured"
    assert s["logged_in"] is False
    assert "reason" in s


def test_status_http_logged_in_when_healthz_returns_logged_in_true(monkeypatch):
    """PREP_AGENT_URL set + healthz returns {"logged_in": true} →
    kind=http, logged_in=true; passthrough fields surfaced."""
    import importlib

    st = importlib.import_module("prep.agent.status")

    monkeypatch.setenv("PREP_AGENT_URL", "http://agent.test")

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(
                {
                    "logged_in": True,
                    "email": "user@example.com",
                    "subscription_type": "max",
                }
            ).encode("utf-8")

    def fake_urlopen(_req, timeout=2.0):
        return FakeResp()

    monkeypatch.setattr(st.urllib.request, "urlopen", fake_urlopen)
    s = st.status()
    assert s["kind"] == "http"
    assert s["logged_in"] is True
    assert s["email"] == "user@example.com"
    assert s["subscription_type"] == "max"


def test_status_http_records_reason_on_unreachable(monkeypatch):
    """PREP_AGENT_URL set but healthz raises → kind=http,
    logged_in=False, reason populated. Probe never explodes upward."""
    import importlib

    st = importlib.import_module("prep.agent.status")

    monkeypatch.setenv("PREP_AGENT_URL", "http://agent.test")

    def boom(_req, timeout=2.0):
        raise OSError("connection refused")

    monkeypatch.setattr(st.urllib.request, "urlopen", boom)
    s = st.status()
    assert s["kind"] == "http"
    assert s["logged_in"] is False
    assert "agent-server unreachable" in s["reason"]


def test_status_http_records_reason_on_non_json(monkeypatch):
    """healthz returns 200 but the body isn't JSON → still graceful."""
    import importlib

    st = importlib.import_module("prep.agent.status")

    monkeypatch.setenv("PREP_AGENT_URL", "http://agent.test")

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"<html>not json</html>"

    monkeypatch.setattr(st.urllib.request, "urlopen", lambda _r, timeout=2.0: FakeResp())
    s = st.status()
    assert s["kind"] == "http"
    assert s["logged_in"] is False
    assert s["reason"] == "agent-server returned non-JSON"


def test_status_shell_logged_in_when_bin_executable(monkeypatch, tmp_path):
    """PREP_AGENT_URL unset + PREP_AGENT_BIN points at an executable
    file → kind=shell, logged_in=true."""
    import importlib

    st = importlib.import_module("prep.agent.status")

    fake_bin = tmp_path / "claude"
    fake_bin.write_text("#!/bin/sh\nexit 0\n")
    fake_bin.chmod(0o755)
    monkeypatch.delenv("PREP_AGENT_URL", raising=False)
    monkeypatch.setenv("PREP_AGENT_BIN", str(fake_bin))

    s = st.status()
    assert s["kind"] == "shell"
    assert s["logged_in"] is True


def test_set_available_and_probe(monkeypatch):
    """`set_available` is the only blessed way to mutate the cached
    flag templates read; `probe()` returns the boolean view of the
    current status."""
    import importlib

    st = importlib.import_module("prep.agent.status")

    st.set_available(True)
    assert st.is_available is True
    st.set_available(False)
    assert st.is_available is False

    # probe wraps status() with a bool view.
    monkeypatch.setattr(st, "status", lambda: {"logged_in": True})
    assert st.probe() is True
    monkeypatch.setattr(st, "status", lambda: {"logged_in": False})
    assert st.probe() is False
