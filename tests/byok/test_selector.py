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
    """Stop deploy-level credentials (OAuth token, free-tier env) from
    leaking into selector tests. Set per-test when the suite actually
    wants them."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    for var in (
        "PREP_FREE_INFERENCE_BASE_URL",
        "PREP_FREE_INFERENCE_API_KEY",
        "PREP_FREE_INFERENCE_MODEL",
        "PREP_FREE_INFERENCE_EXTRA_BODY",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture
def _free_tier_env(monkeypatch):
    """Configure a complete free tier via env."""
    monkeypatch.setenv("PREP_FREE_INFERENCE_BASE_URL", "https://inference.example/v1")
    monkeypatch.setenv("PREP_FREE_INFERENCE_API_KEY", "free-tier-key")
    monkeypatch.setenv("PREP_FREE_INFERENCE_MODEL", "test-free-model")


@pytest.fixture
def _byok_master(monkeypatch):
    """BYOKRepo() (used by the selector under default factory) needs
    PREP_KEY_ENCRYPTION_SECRET in env. Inject a deterministic key."""
    monkeypatch.setenv("PREP_KEY_ENCRYPTION_SECRET", _KEY.hex())


@pytest.fixture
def _prep_logs_propagate(monkeypatch):
    """prep/app.py gives the `prep` logger its own handler and sets
    propagate=False, so once any test imports the app, caplog's
    root-level handler never sees selector records. Re-enable
    propagation for tests that assert on caplog."""
    import logging

    monkeypatch.setattr(logging.getLogger("prep"), "propagate", True)


def test_returns_noop_when_no_oauth_and_no_byok(initialized_db):
    """Empty deploy + anonymous caller → noop adapter that raises."""
    agent = selector.agent_for_user(None)
    assert agent.__class__.__name__ == "_NoopAgent"
    import asyncio

    with pytest.raises(AgentUnavailable):
        asyncio.run(agent.run("hi"))


def test_falls_through_to_subscription_oauth_when_no_byok(
    monkeypatch, initialized_db, _byok_master
):
    """User has no BYOK row but the deploy has a subscription token →
    we return the SDK adapter (the pre-BYOK path). Only a clean
    "no row" continues past the BYOK step now, so the master key must
    be configured for the lookup to complete."""
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


def test_byok_failure_returns_noop_never_another_credential(
    monkeypatch, initialized_db, _byok_master, _prep_logs_propagate, caplog
):
    """A BYOK-step exception (corrupt row, master rotated, decrypt
    failure) must surface as Noop - never fall through to another
    credential. Silently switching credentials would mask the broken
    key, and with the free tier configured it would be a silent
    privacy downgrade. The failure is logged loudly for the operator.

    A stored row is seeded first: the selector's decrypt-free
    row-existence check deliberately lets row-less users fall
    through (see test_masterkeyless_deploy_still_gets_deploy_wide_ai),
    so the fail-closed guarantee is scoped to users who HAVE a
    credential."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fake-test-token")
    uid = "anyuser@example.com"
    UserRepo().upsert(external_id=uid, email=uid)
    BYOKRepo().store(user_id=uid, provider=Provider.ANTHROPIC_API, secret="sk-ant-api03-mine")

    def boom(self, *a, **kw):
        raise RuntimeError("simulated BYOK failure")

    monkeypatch.setattr(BYOKRepo, "get_secret", boom)

    agent = selector.agent_for_user(uid)
    assert agent.__class__.__name__ == "_NoopAgent"
    assert "BYOK path failed" in caplog.text


def test_subscription_path_blocked_on_clerk_mode(monkeypatch, initialized_db, _byok_master):
    """Cut 1: a stray CLAUDE_CODE_OAUTH_TOKEN on a multi-user clerk
    deploy must NOT silently fund every user's AI from the operator's
    Max credit pool. Gating happens in _subscription_path_allowed();
    when off (and no free tier is configured), the selector falls all
    the way through to noop."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-leftover-from-local-dev")
    monkeypatch.setenv("PREP_AUTH_MODE", "clerk")

    agent = selector.agent_for_user("random-clerk-user@example.com")
    assert agent.__class__.__name__ == "_NoopAgent"


def test_a_retired_subscription_row_selects_nothing(monkeypatch, initialized_db, _byok_master):
    """The provider is retired (decision 7.4): a row stored before the
    retirement must not build an adapter, and must not shadow a real
    API key the same user holds."""
    uid = "sub-byok@example.com"
    UserRepo().upsert(external_id=uid, email=uid)
    BYOKRepo().store(
        user_id=uid,
        provider=Provider.CLAUDE_SUBSCRIPTION,
        secret="sk-ant-oat01-pretend-this-came-from-claude-setup-token",
    )

    agent = selector.agent_for_user(uid)
    assert agent.__class__.__name__ == "_NoopAgent"


def test_an_api_key_is_chosen_over_a_retired_subscription_row(
    monkeypatch, initialized_db, _byok_master
):
    uid = "sub-vs-api@example.com"
    UserRepo().upsert(external_id=uid, email=uid)
    repo = BYOKRepo()
    repo.store(user_id=uid, provider=Provider.ANTHROPIC_API, secret="sk-ant-api03-metered")
    repo.store(user_id=uid, provider=Provider.CLAUDE_SUBSCRIPTION, secret="sk-ant-oat01-flatrate")

    agent = selector.agent_for_user(uid)
    assert agent.__class__.__name__ == "AnthropicApiAdapter"


def test_agent_available_for_user_true_on_byok(monkeypatch, initialized_db, _byok_master):
    """The per-user availability helper drives the `agent_available`
    template flag. The bug it guards: a clerk-mode user who saved a
    BYOK key still saw "no AI configured", because the legacy
    module-level probe is file-presence-only and misses BYOK rows."""
    uid = "byok-only@example.com"
    UserRepo().upsert(external_id=uid, email=uid)
    BYOKRepo().store(
        user_id=uid,
        provider=Provider.ANTHROPIC_API,
        secret="sk-ant-api03-byokscoped",
    )
    assert selector.agent_available_for_user(uid) is True


def test_agent_available_for_user_false_when_nothing_configured(
    monkeypatch, initialized_db, _byok_master
):
    """No env token, no BYOK row, no free tier → False. Drives the
    manual-flashcard fallback UI."""
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


# ---- free-tier factory contract ------------------------------------------
#
# The property under test throughout: free_tier_agent() NEVER raises.
# It runs (via agent_for_user) inside the template context processor
# on every page render, so an escaping exception is a deploy-wide 500.


def _error_records(caplog):
    import logging

    return [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_free_tier_factory_none_and_silent_when_unset(_prep_logs_propagate, caplog):
    """All env unset = feature off. No adapter, and no ERROR noise -
    unset is the normal state, not an operator mistake."""
    assert selector.free_tier_agent() is None
    assert selector.free_tier_configured() is False
    assert _error_records(caplog) == []


def test_free_tier_factory_returns_shared_adapter(_free_tier_env):
    from prep.agent.openai_compat import OpenAICompatAdapter

    agent = selector.free_tier_agent()
    assert isinstance(agent, OpenAICompatAdapter)
    assert agent._shared is True
    assert agent._base_url == "https://inference.example/v1"
    assert agent._model == "test-free-model"
    assert agent._provider_label == "free AI"
    # Transform-safe output cap: well above the 4096 BYOK default.
    assert agent._max_tokens > 4096
    assert selector.free_tier_configured() is True


def test_free_tier_factory_parses_extra_body_once_at_factory_time(monkeypatch, _free_tier_env):
    monkeypatch.setenv(
        "PREP_FREE_INFERENCE_EXTRA_BODY",
        '{"chat_template_kwargs": {"enable_thinking": false}}',
    )
    agent = selector.free_tier_agent()
    assert agent._extra_body == {"chat_template_kwargs": {"enable_thinking": False}}


def test_free_tier_factory_partial_config_logs_and_disables(
    monkeypatch, _prep_logs_propagate, caplog
):
    """A half-configured free tier is operator error: ERROR log naming
    the missing vars, feature off."""
    monkeypatch.setenv("PREP_FREE_INFERENCE_BASE_URL", "https://inference.example/v1")
    assert selector.free_tier_agent() is None
    assert selector.free_tier_configured() is False
    errors = _error_records(caplog)
    assert errors
    assert "PREP_FREE_INFERENCE_API_KEY" in caplog.text
    assert "PREP_FREE_INFERENCE_MODEL" in caplog.text


def test_free_tier_factory_malformed_extra_body_logs_and_disables(
    monkeypatch, _free_tier_env, _prep_logs_propagate, caplog
):
    monkeypatch.setenv("PREP_FREE_INFERENCE_EXTRA_BODY", "{not json")
    assert selector.free_tier_agent() is None
    assert "PREP_FREE_INFERENCE_EXTRA_BODY" in caplog.text
    assert _error_records(caplog)


def test_free_tier_factory_non_object_extra_body_logs_and_disables(
    monkeypatch, _free_tier_env, _prep_logs_propagate, caplog
):
    """Valid JSON that isn't an object is still a config error."""
    monkeypatch.setenv("PREP_FREE_INFERENCE_EXTRA_BODY", '["a", "list"]')
    assert selector.free_tier_agent() is None
    assert "PREP_FREE_INFERENCE_EXTRA_BODY" in caplog.text
    assert _error_records(caplog)


def test_free_tier_factory_constructor_exception_logs_and_disables(
    monkeypatch, _free_tier_env, _prep_logs_propagate, caplog
):
    from prep.agent.openai_compat import OpenAICompatAdapter

    def boom(self, *a, **kw):
        raise RuntimeError("simulated constructor failure")

    monkeypatch.setattr(OpenAICompatAdapter, "__init__", boom)
    assert selector.free_tier_agent() is None
    assert _error_records(caplog)


def test_free_tier_factory_never_raises_on_unexpected_failure(
    monkeypatch, _free_tier_env, _prep_logs_propagate, caplog
):
    """The belt over the branches: an exception shape the factory
    doesn't specifically handle still converts to None + ERROR."""
    monkeypatch.setenv("PREP_FREE_INFERENCE_EXTRA_BODY", "{}")

    def boom(*a, **kw):
        raise RuntimeError("not a ValueError - escapes the parse branch")

    monkeypatch.setattr(selector.json, "loads", boom)
    assert selector.free_tier_agent() is None
    assert _error_records(caplog)


# ---- selector precedence with the free tier ------------------------------


def test_free_tier_selected_when_no_byok(initialized_db, _byok_master, _free_tier_env):
    """No BYOK row, no subscription token → the free tier serves."""
    from prep.agent.openai_compat import OpenAICompatAdapter

    agent = selector.agent_for_user("nobyok-free@example.com")
    assert isinstance(agent, OpenAICompatAdapter)
    assert agent._shared is True


def test_byok_beats_free_tier(initialized_db, _byok_master, _free_tier_env):
    """The user's own key always wins - nobody is silently downgraded
    to the shared free model after configuring their own provider."""
    uid = "byok-over-free@example.com"
    UserRepo().upsert(external_id=uid, email=uid)
    BYOKRepo().store(user_id=uid, provider=Provider.ANTHROPIC_API, secret="sk-ant-api03-mine")
    agent = selector.agent_for_user(uid)
    assert isinstance(agent, AnthropicApiAdapter)


def test_byok_failure_lands_on_noop_not_free_tier(
    monkeypatch, initialized_db, _byok_master, _free_tier_env, _prep_logs_propagate, caplog
):
    """THE privacy pin: BYOK row present + decrypt raises + free tier
    configured → Noop, NOT the free adapter. BYOK is the privacy
    opt-out from the shared endpoint; a broken key path must never
    silently change which provider sees the user's prompts."""
    uid = "decrypt-broken@example.com"
    UserRepo().upsert(external_id=uid, email=uid)
    BYOKRepo().store(user_id=uid, provider=Provider.ANTHROPIC_API, secret="sk-ant-api03-mine")

    def boom(self, *a, **kw):
        raise RuntimeError("simulated decrypt failure")

    monkeypatch.setattr(BYOKRepo, "get_secret", boom)
    agent = selector.agent_for_user(uid)
    assert agent.__class__.__name__ == "_NoopAgent"
    assert "BYOK path failed" in caplog.text


def test_clerk_gate_still_beats_stray_token_free_tier_serves(
    monkeypatch, initialized_db, _byok_master, _free_tier_env
):
    """Clerk mode + stray CLAUDE_CODE_OAUTH_TOKEN + free configured →
    free tier. The subscription hard-gate still wins over the token;
    the free tier is the legal deploy-wide fallback."""
    from prep.agent.openai_compat import OpenAICompatAdapter

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-leftover")
    monkeypatch.setenv("PREP_AUTH_MODE", "clerk")
    agent = selector.agent_for_user("clerk-user@example.com")
    assert isinstance(agent, OpenAICompatAdapter)
    assert agent._shared is True


def test_subscription_outranks_free_tier_on_single_user_installs(
    monkeypatch, initialized_db, _byok_master, _free_tier_env
):
    """Tailscale install + token + free configured → SDK adapter. The
    operator opted into the subscription; the flat-rate pool is
    theirs to burn."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-operators-own")
    agent = selector.agent_for_user("operator@example.com")
    assert isinstance(agent, ClaudeAgentSdkAdapter)


def test_user_id_none_may_land_on_free_tier(initialized_db, _free_tier_env):
    """System-initiated calls (user_id=None) skip BYOK and may be
    served by the free tier."""
    from prep.agent.openai_compat import OpenAICompatAdapter

    agent = selector.agent_for_user(None)
    assert isinstance(agent, OpenAICompatAdapter)
    assert agent._shared is True


def test_agent_available_for_user_true_on_free_tier_only(
    initialized_db, _byok_master, _free_tier_env
):
    """The `agent_available` template flag lights up on a free-only
    deploy - zero-setup AI is the whole point."""
    assert selector.agent_available_for_user("fresh-signup@example.com") is True


def test_masterkeyless_deploy_still_gets_deploy_wide_ai(initialized_db, monkeypatch):
    """A deploy that never configured PREP_KEY_ENCRYPTION_SECRET must
    not lose deploy-wide AI. BYOKRepo() raises MasterKeyError eagerly,
    but a user with NO stored credential row has nothing a downgrade
    could leak, so the selector checks row existence decrypt-free and
    falls through to the free tier / subscription path.

    The privacy pin (test_byok_failure_lands_on_noop_not_free_tier)
    stays intact: a user WITH a row still fails closed."""
    from prep.agent import selector

    monkeypatch.delenv("PREP_KEY_ENCRYPTION_SECRET", raising=False)
    monkeypatch.setenv("PREP_FREE_INFERENCE_BASE_URL", "https://inference.example.com/api/v1")
    monkeypatch.setenv("PREP_FREE_INFERENCE_API_KEY", "freekey123")
    monkeypatch.setenv("PREP_FREE_INFERENCE_MODEL", "test-model")

    agent = selector.agent_for_user("nobody-with-rows@example.com")
    # Must be the free-tier adapter, not the Noop fail-closed stand-in.
    assert type(agent).__name__ == "OpenAICompatAdapter", type(agent).__name__
