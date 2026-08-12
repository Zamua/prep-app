"""free_tier_agent() output-cap parameter: an explicit
max_output_tokens overrides the transform-sized default; omitting it
keeps the default for existing callers."""

from __future__ import annotations

import pytest

from prep.agent import selector
from prep.agent.openai_compat import OpenAICompatAdapter


@pytest.fixture
def _free_tier_env(monkeypatch):
    monkeypatch.setenv("PREP_FREE_INFERENCE_BASE_URL", "https://inference.example/v1")
    monkeypatch.setenv("PREP_FREE_INFERENCE_API_KEY", "free-tier-key")
    monkeypatch.setenv("PREP_FREE_INFERENCE_MODEL", "test-free-model")
    monkeypatch.delenv("PREP_FREE_INFERENCE_EXTRA_BODY", raising=False)


def test_default_keeps_transform_sized_cap(_free_tier_env):
    agent = selector.free_tier_agent()
    assert isinstance(agent, OpenAICompatAdapter)
    assert agent._max_tokens == selector._FREE_TIER_MAX_TOKENS


def test_explicit_cap_overrides_default(_free_tier_env):
    agent = selector.free_tier_agent(max_output_tokens=2048)
    assert isinstance(agent, OpenAICompatAdapter)
    assert agent._max_tokens == 2048


def test_none_is_the_default_cap(_free_tier_env):
    explicit = selector.free_tier_agent(max_output_tokens=None)
    implicit = selector.free_tier_agent()
    assert explicit._max_tokens == implicit._max_tokens == selector._FREE_TIER_MAX_TOKENS


def test_unconfigured_returns_none_regardless_of_cap(monkeypatch):
    for var in (
        "PREP_FREE_INFERENCE_BASE_URL",
        "PREP_FREE_INFERENCE_API_KEY",
        "PREP_FREE_INFERENCE_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    assert selector.free_tier_agent(max_output_tokens=2048) is None
