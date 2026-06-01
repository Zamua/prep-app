"""Provider-detection + multi-provider selector tests."""

from __future__ import annotations

import pytest

from prep.agent import selector
from prep.agent.anthropic_api import AnthropicApiAdapter
from prep.agent.openai_api import OpenAIAdapter
from prep.agent.openrouter import OpenRouterAdapter
from prep.auth.repo import UserRepo
from prep.byok.entities import PROVIDERS, Provider, provider_for_key
from prep.byok.repo import BYOKRepo

_KEY = bytes.fromhex("ee" * 32)


@pytest.fixture(autouse=True)
def _scrub_oauth_env(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)


@pytest.fixture
def _byok_master(monkeypatch):
    monkeypatch.setenv("PREP_KEY_ENCRYPTION_SECRET", _KEY.hex())


# ---- provider_for_key disambiguation -------------------------------------


@pytest.mark.parametrize(
    "secret, expected",
    [
        ("sk-ant-api03-abcdef", Provider.ANTHROPIC_API),
        ("sk-or-v1-abcdef1234", Provider.OPENROUTER_API),
        ("sk-proj-abcdef1234", Provider.OPENAI_API),
        ("sk-abcdef1234", Provider.OPENAI_API),
        ("not-a-key", None),
        ("", None),
    ],
)
def test_provider_for_key_disambiguates_by_prefix(secret, expected):
    assert provider_for_key(secret) is expected


# ---- selector precedence over multiple providers -------------------------


def test_selector_prefers_anthropic_when_multiple_keys_configured(initialized_db, _byok_master):
    uid = "multi@example.com"
    UserRepo().upsert(external_id=uid, email=uid)
    repo = BYOKRepo()
    repo.store(user_id=uid, provider=Provider.OPENAI_API, secret="sk-proj-openai-zzz")
    repo.store(user_id=uid, provider=Provider.OPENROUTER_API, secret="sk-or-v1-router-zzz")
    repo.store(user_id=uid, provider=Provider.ANTHROPIC_API, secret="sk-ant-api03-claude-zzz")

    agent = selector.agent_for_user(uid)
    assert isinstance(agent, AnthropicApiAdapter)


def test_selector_uses_openrouter_when_only_openrouter_is_configured(initialized_db, _byok_master):
    uid = "only-or@example.com"
    UserRepo().upsert(external_id=uid, email=uid)
    BYOKRepo().store(user_id=uid, provider=Provider.OPENROUTER_API, secret="sk-or-v1-router-zzz")

    agent = selector.agent_for_user(uid)
    assert isinstance(agent, OpenRouterAdapter)


def test_selector_uses_openai_when_only_openai_is_configured(initialized_db, _byok_master):
    uid = "only-oai@example.com"
    UserRepo().upsert(external_id=uid, email=uid)
    BYOKRepo().store(user_id=uid, provider=Provider.OPENAI_API, secret="sk-proj-openai-zzz")

    agent = selector.agent_for_user(uid)
    assert isinstance(agent, OpenAIAdapter)


def test_selector_openrouter_beats_openai_when_both_present(initialized_db, _byok_master):
    """OpenRouter is the multi-vendor router; pick it over plain OpenAI
    because it can also access OpenAI's models. Same intent as the
    Anthropic-over-OpenRouter precedence — narrower wins."""
    uid = "or-vs-oai@example.com"
    UserRepo().upsert(external_id=uid, email=uid)
    repo = BYOKRepo()
    repo.store(user_id=uid, provider=Provider.OPENAI_API, secret="sk-proj-openai")
    repo.store(user_id=uid, provider=Provider.OPENROUTER_API, secret="sk-or-v1-router")

    agent = selector.agent_for_user(uid)
    assert isinstance(agent, OpenRouterAdapter)


# ---- provider metadata wiring -------------------------------------------


def test_all_providers_have_metadata():
    """Belt-and-suspenders: every Provider enum value must have a
    corresponding PROVIDERS entry. Catches the 'added enum, forgot
    metadata' bug."""
    for p in Provider:
        assert p in PROVIDERS, f"missing metadata for {p}"
        info = PROVIDERS[p]
        assert info.label
        assert info.key_prefixes
        assert info.console_url.startswith("https://")
        assert info.default_model
