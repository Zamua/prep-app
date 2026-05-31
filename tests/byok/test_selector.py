"""Selector precedence tests — BYOK > subscription OAuth > Noop.

Exercises the same dispatch the route handlers rely on, with no real
network calls. Each path mocks just the thing it's verifying so a
break in one selection layer doesn't cascade through siblings.
"""

from __future__ import annotations

import pytest

from prep.agent import selector
from prep.agent.anthropic_api import AnthropicApiAdapter
from prep.agent.port import AgentUnavailable
from prep.agent.sdk_adapter import ClaudeAgentSdkAdapter
from prep.auth.repo import UserRepo
from prep.byok.entities import Provider
from prep.byok.repo import BYOKRepo

_KEY = bytes.fromhex("cc" * 32)


@pytest.fixture(autouse=True)
def _scrub_oauth_env(monkeypatch):
    """Stop the deploy-level OAuth token from leaking into selector
    tests. Set per-test when the suite actually wants it."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    yield


@pytest.fixture
def _byok_master(monkeypatch):
    """BYOKRepo() (used by the selector under default factory) needs
    PREP_KEY_ENCRYPTION_SECRET in env. Inject a deterministic key."""
    monkeypatch.setenv("PREP_KEY_ENCRYPTION_SECRET", _KEY.hex())


def test_returns_noop_when_no_oauth_and_no_byok(initialized_db):
    """Empty deploy + anonymous caller → noop adapter that raises."""
    agent = selector.agent_for_user(None)
    assert agent.__class__.__name__ == "_NoopAgent"
    import asyncio

    with pytest.raises(AgentUnavailable):
        asyncio.run(agent.run("hi"))


def test_falls_through_to_subscription_oauth_when_no_byok(monkeypatch, initialized_db):
    """User has no BYOK row but the deploy has a subscription token →
    we return the SDK adapter (the pre-BYOK path)."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fake-test-token")
    agent = selector.agent_for_user("nobyok@example.com")
    assert isinstance(agent, ClaudeAgentSdkAdapter)


def test_byok_takes_precedence_over_subscription(monkeypatch, initialized_db, _byok_master):
    """When BOTH a BYOK key AND the OAuth token are set, BYOK wins —
    that's the user's explicit choice; never silently bill the
    shared subscription pool when they've configured their own."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fake-test-token")

    UserRepo().upsert(external_id="byok-precedence@example.com", email="x@example.com")
    BYOKRepo().store(
        user_id="byok-precedence@example.com",
        provider=Provider.ANTHROPIC_API,
        secret="sk-ant-api03-user-key-zzz",
    )
    agent = selector.agent_for_user("byok-precedence@example.com")
    assert isinstance(agent, AnthropicApiAdapter)


def test_byok_lookup_failure_does_not_break_other_users(monkeypatch, initialized_db, caplog):
    """If BYOK breaks for a specific user (corrupt row, master rotated,
    etc.) the selector should log + fall through to the subscription
    path rather than crash. Belt-and-suspenders — protects everyone
    else from one user's BYOK problem."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fake-test-token")

    def boom(self, *a, **kw):
        raise RuntimeError("simulated BYOK failure")

    monkeypatch.setattr(BYOKRepo, "get_secret", boom)

    agent = selector.agent_for_user("anyuser@example.com")
    # Fell through to the subscription path, not noop.
    assert isinstance(agent, ClaudeAgentSdkAdapter)


def test_factory_override_short_circuits_selection(initialized_db):
    """Tests can inject any adapter via set_user_agent_factory and
    the selector hands it back regardless of DB/env state."""
    from prep.agent.port import AgentResult

    class _Stub:
        async def run(self, *a, **kw):
            return AgentResult(text="stub", model="stub")

    captured: list = []

    def factory(uid):
        captured.append(uid)
        return _Stub()

    selector.set_user_agent_factory(factory)
    try:
        agent = selector.agent_for_user("anyone@example.com")
    finally:
        selector.set_user_agent_factory(None)
    assert isinstance(agent, _Stub)
    assert captured == ["anyone@example.com"]
