"""Route tests for the agent context.

Three endpoints under /settings/agent — view, connect, disconnect.
The connect/disconnect routes shell out to a separate agent-server
container; we monkeypatch the urlopen call so the tests don't need
docker.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient


def test_settings_agent_view_renders(client: TestClient, initialized_db: str, monkeypatch):
    """GET /settings/agent renders even with no agent configured."""
    monkeypatch.delenv("PREP_AGENT_URL", raising=False)
    monkeypatch.delenv("PREP_AGENT_BIN", raising=False)
    r = client.get("/settings/agent")
    assert r.status_code == 200


def test_settings_agent_connect_requires_url_env(
    client: TestClient, initialized_db: str, monkeypatch
):
    """Without PREP_AGENT_URL set, connect can't reach an agent-server.
    Route returns 400 with a friendly explanation rather than 500."""
    monkeypatch.delenv("PREP_AGENT_URL", raising=False)
    r = client.post("/settings/agent/connect", data={"token": "sk-ant-oat01-fake"})
    assert r.status_code == 400


def test_settings_agent_connect_rejects_missing_token(
    client: TestClient, initialized_db: str, monkeypatch
):
    """Empty token → 400 before we even attempt to reach the
    agent-server."""
    monkeypatch.setenv("PREP_AGENT_URL", "http://agent.test")
    r = client.post("/settings/agent/connect", data={"token": "   "})
    assert r.status_code == 400


def test_settings_agent_connect_forwards_token(
    client: TestClient, initialized_db: str, monkeypatch
):
    """Happy path: PREP_AGENT_URL set + agent-server returns 200 →
    route returns 200 and forwards the token in the JSON body."""
    monkeypatch.setenv("PREP_AGENT_URL", "http://agent.test")

    captured: dict = {}

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=10.0):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResp()

    # Stub the agent module's status() so the post-connect refresh
    # doesn't try to actually probe.
    from prep import agent as agent_pkg

    monkeypatch.setattr(agent_pkg, "status", lambda: {"kind": "http", "logged_in": True})

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    r = client.post("/settings/agent/connect", data={"token": "sk-ant-oat01-fake"})
    assert r.status_code == 200
    assert captured["url"] == "http://agent.test/connect"
    assert captured["body"] == {"token": "sk-ant-oat01-fake"}
