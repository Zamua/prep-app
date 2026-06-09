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


def test_subscription_path_blocked_on_clerk_mode(monkeypatch, initialized_db):
    """Cut 1: a stray CLAUDE_CODE_OAUTH_TOKEN on a multi-user clerk
    deploy must NOT silently fund every user's AI from the operator's
    Max credit pool. Gating happens in _subscription_path_allowed();
    when off, the selector falls all the way through to noop."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-leftover-from-local-dev")
    monkeypatch.setenv("PREP_AUTH_MODE", "clerk")

    agent = selector.agent_for_user("random-clerk-user@example.com")
    assert agent.__class__.__name__ == "_NoopAgent"


def test_subscription_byok_builds_sdk_adapter_with_bound_token(
    monkeypatch, initialized_db, _byok_master
):
    """Cut 2: a per-user oat01 token stored as CLAUDE_SUBSCRIPTION must
    materialize as ClaudeAgentSdkAdapter with the user's token bound,
    so the SDK sees it via options.env instead of process env. This
    is what makes the multi-user case concurrency-safe."""
    uid = "sub-byok@example.com"
    UserRepo().upsert(external_id=uid, email=uid)
    user_token = "sk-ant-oat01-pretend-this-came-from-claude-setup-token"
    BYOKRepo().store(user_id=uid, provider=Provider.CLAUDE_SUBSCRIPTION, secret=user_token)

    agent = selector.agent_for_user(uid)
    assert isinstance(agent, ClaudeAgentSdkAdapter)
    # Token is bound to the adapter instance, not lifted into os.environ.
    assert agent._token == user_token
    import os as _os

    assert _os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") is None


def test_subscription_byok_wins_over_anthropic_api_by_default(
    monkeypatch, initialized_db, _byok_master
):
    """Cut 2 precedence: when a user has both a sk-ant-oat01 (flat-rate
    subscription pool) and a sk-ant-api03 (per-token metered API key)
    configured, default to subscription so they don't get surprised
    by metered API charges. Explicit active_byok_provider still wins."""
    uid = "sub-vs-api@example.com"
    UserRepo().upsert(external_id=uid, email=uid)
    repo = BYOKRepo()
    repo.store(user_id=uid, provider=Provider.ANTHROPIC_API, secret="sk-ant-api03-metered")
    repo.store(user_id=uid, provider=Provider.CLAUDE_SUBSCRIPTION, secret="sk-ant-oat01-flatrate")

    agent = selector.agent_for_user(uid)
    assert isinstance(agent, ClaudeAgentSdkAdapter)
    assert agent._token == "sk-ant-oat01-flatrate"

    # Explicit choice flips it back to the API key.
    UserRepo().set_active_byok_provider(uid, Provider.ANTHROPIC_API.value)
    flipped = selector.agent_for_user(uid)
    assert flipped.__class__.__name__ == "AnthropicApiAdapter"


def test_agent_available_for_user_true_on_byok(monkeypatch, initialized_db, _byok_master):
    """The per-user availability helper drives the `agent_available`
    template flag. The bug: without this, a clerk-mode user who saved
    their subscription token via BYOK still saw "no AI configured"
    because the legacy module-level probe is file-presence-only and
    misses BYOK rows. Verify the helper actually returns True in that
    setup."""
    uid = "byok-only@example.com"
    UserRepo().upsert(external_id=uid, email=uid)
    BYOKRepo().store(
        user_id=uid,
        provider=Provider.CLAUDE_SUBSCRIPTION,
        secret="sk-ant-oat01-byokscoped",
    )
    assert selector.agent_available_for_user(uid) is True


def test_agent_available_for_user_false_when_nothing_configured(monkeypatch, initialized_db):
    """No env token, no BYOK row → False. Drives the manual-flashcard
    fallback UI."""
    assert selector.agent_available_for_user("nobody@example.com") is False


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
