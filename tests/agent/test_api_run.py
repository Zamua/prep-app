"""Tests for the /api/agent/run machine-to-machine SDK endpoint.

The endpoint speaks the same wire format the legacy agent-server
/run uses ({prompt, session_id?, resume_id?} → {stdout}) so the Go
worker can swap its BaseURL without code changes. We use FakeAgent
via set_agent() so the suite never burns real SDK credits.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from prep import agent as _agent_mod
from prep.agent.fake import FakeAgent
from prep.agent.port import AgentResult
from prep.agent.usage import AgentUsageRepo, hash_token


@pytest.fixture
def fake_agent(monkeypatch: pytest.MonkeyPatch) -> FakeAgent:
    """Swap the process-singleton AgentPort with a FakeAgent for the
    duration of a test. set_agent() puts the original back via the
    monkeypatch.undo() that pytest runs at teardown."""
    original = _agent_mod.get_agent()
    fake = FakeAgent()
    _agent_mod.set_agent(fake)
    yield fake
    _agent_mod.set_agent(original)


@pytest.fixture
def internal_token(monkeypatch: pytest.MonkeyPatch) -> str:
    token = "test-internal-secret"
    monkeypatch.setenv("PREP_INTERNAL_TOKEN", token)
    return token


@pytest.fixture
def oauth_token(monkeypatch: pytest.MonkeyPatch) -> str:
    token = "sk-ant-oat01-test-fake-oauth-token"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", token)
    return token


def test_run_returns_stdout_wire_format(
    client: TestClient, fake_agent: FakeAgent, internal_token: str
):
    """The Go worker speaks {prompt} → {stdout}. New endpoint must
    match exactly so the BaseURL swap is a no-op on the worker side."""
    fake_agent.next_response = AgentResult(
        text="hello from the SDK", model="claude-sonnet-4-6", cost_usd=0.001
    )
    r = client.post(
        "/api/agent/run",
        json={"prompt": "say hi"},
        headers={"X-Internal-Token": internal_token},
    )
    assert r.status_code == 200
    assert r.json() == {"stdout": "hello from the SDK"}
    # The fake recorded the call so we know the adapter was invoked.
    assert len(fake_agent.calls) == 1
    assert fake_agent.calls[0]["prompt"] == "say hi"


def test_run_requires_internal_token(
    client: TestClient, fake_agent: FakeAgent, internal_token: str
):
    r = client.post("/api/agent/run", json={"prompt": "x"})
    assert r.status_code == 401


def test_run_503_when_internal_token_unconfigured(
    client: TestClient, fake_agent: FakeAgent, monkeypatch: pytest.MonkeyPatch
):
    """Fail-closed: no env var configured = service refuses everything.
    Prevents accidentally exposing the endpoint with no auth."""
    monkeypatch.delenv("PREP_INTERNAL_TOKEN", raising=False)
    r = client.post(
        "/api/agent/run",
        json={"prompt": "x"},
        headers={"X-Internal-Token": "anything"},
    )
    assert r.status_code == 503


def test_run_502_when_agent_unavailable(
    client: TestClient, fake_agent: FakeAgent, internal_token: str
):
    """Adapter raising AgentUnavailable maps to 502 + {error}."""
    fake_agent.raise_unavailable = True
    r = client.post(
        "/api/agent/run",
        json={"prompt": "x"},
        headers={"X-Internal-Token": internal_token},
    )
    assert r.status_code == 502
    assert "error" in r.json()


def test_run_logs_usage_keyed_on_token_hash(
    client: TestClient,
    fake_agent: FakeAgent,
    internal_token: str,
    oauth_token: str,
    initialized_db: str,
):
    """Every successful call lands one row in agent_usage, keyed on
    the sha256 of CLAUDE_CODE_OAUTH_TOKEN, with the adapter-reported
    cost. user_login is null for worker calls."""
    fake_agent.next_response = AgentResult(
        text="ok",
        model="claude-sonnet-4-6",
        input_tokens=42,
        output_tokens=8,
        cost_usd=0.0123,
    )
    r = client.post(
        "/api/agent/run",
        json={"prompt": "hi"},
        headers={"X-Internal-Token": internal_token},
    )
    assert r.status_code == 200

    from datetime import datetime, timedelta, timezone

    repo = AgentUsageRepo()
    th = hash_token(oauth_token)
    window = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
    assert repo.call_count(th, month_start_iso=window) == 1
    assert abs(repo.monthly_cost(th, month_start_iso=window) - 0.0123) < 1e-9


def test_run_passes_through_model_and_reasoning_overrides(
    client: TestClient, fake_agent: FakeAgent, internal_token: str
):
    """When the worker overrides model/reasoning, the adapter sees
    those exact values (not the adapter defaults)."""
    client.post(
        "/api/agent/run",
        json={"prompt": "x", "model": "claude-haiku-4-5", "reasoning": "high"},
        headers={"X-Internal-Token": internal_token},
    )
    assert fake_agent.calls[-1]["model"] == "claude-haiku-4-5"
    assert fake_agent.calls[-1]["reasoning"] == "high"


def test_run_ignores_session_id_resume_id(
    client: TestClient, fake_agent: FakeAgent, internal_token: str
):
    """Legacy fields accepted-but-ignored. Endpoint should still 200."""
    r = client.post(
        "/api/agent/run",
        json={"prompt": "x", "session_id": "abc", "resume_id": "def"},
        headers={"X-Internal-Token": internal_token},
    )
    assert r.status_code == 200
