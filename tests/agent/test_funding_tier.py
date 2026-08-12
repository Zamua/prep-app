"""funding_tier_for_user: the decrypt-free mirror of agent_for_user's
precedence. Pins which tier would fund a call so per-call free-tier
policy (card caps) attaches to exactly the free-funded calls."""

from __future__ import annotations

import pytest

from prep.agent import selector

_FREE_VARS = {
    "PREP_FREE_INFERENCE_BASE_URL": "https://inference.example/v1",
    "PREP_FREE_INFERENCE_API_KEY": "free-tier-key",
    "PREP_FREE_INFERENCE_MODEL": "test-free-model",
}


@pytest.fixture
def free_tier_env(monkeypatch):
    for name, value in _FREE_VARS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("PREP_FREE_INFERENCE_EXTRA_BODY", raising=False)


@pytest.fixture
def no_free_tier_env(monkeypatch):
    for name in _FREE_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PREP_FREE_INFERENCE_EXTRA_BODY", raising=False)


def _insert_byok_row(user_id: str) -> None:
    # Row existence is all the helper reads; the ciphertext never
    # decrypts, so a placeholder blob is enough.
    from prep.infrastructure.db import cursor

    with cursor() as c:
        c.execute(
            "INSERT INTO byok_credentials"
            " (user_id, provider, ciphertext, key_prefix, created_at)"
            " VALUES (?, 'anthropic-api', 'not-real-ciphertext', 'sk-ant-', '2020-01-01T00:00:00Z')",
            (user_id,),
        )


def test_byok_rows_win_over_every_shared_tier(initialized_db, free_tier_env, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.delenv("PREP_AUTH_MODE", raising=False)
    _insert_byok_row(initialized_db)
    assert selector.funding_tier_for_user(initialized_db) == "byok"


def test_row_check_failure_fails_closed_as_byok(free_tier_env, monkeypatch):
    def _raise(_uid):
        raise RuntimeError("row check failed")

    monkeypatch.setattr(selector, "_user_has_byok_rows", _raise)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert selector.funding_tier_for_user("someone@example.com") == "byok"


def test_subscription_on_single_user_install(initialized_db, free_tier_env, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.delenv("PREP_AUTH_MODE", raising=False)
    assert selector.funding_tier_for_user(initialized_db) == "subscription"


def test_clerk_mode_gates_subscription_off(initialized_db, free_tier_env, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("PREP_AUTH_MODE", "clerk")
    assert selector.funding_tier_for_user(initialized_db) == "free"


def test_free_when_only_free_tier_configured(initialized_db, free_tier_env, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert selector.funding_tier_for_user(initialized_db) == "free"


def test_none_when_nothing_configured(initialized_db, no_free_tier_env, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert selector.funding_tier_for_user(initialized_db) == "none"


def test_none_user_skips_byok(no_free_tier_env, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.delenv("PREP_AUTH_MODE", raising=False)
    assert selector.funding_tier_for_user(None) == "subscription"


def test_free_tier_agrees_with_selector_adapter(initialized_db, free_tier_env, monkeypatch):
    """ "free" must mean agent_for_user hands back the shared adapter."""
    from prep.agent.openai_compat import OpenAICompatAdapter

    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert selector.funding_tier_for_user(initialized_db) == "free"
    assert isinstance(selector.agent_for_user(initialized_db), OpenAICompatAdapter)
