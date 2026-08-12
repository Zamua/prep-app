"""Instant generation use cases: topic hygiene, adapter resolution,
outcome mapping, and output hygiene.

The resolution pins are the security contract: the endpoint can only
ever construct the free-tier adapter (with the instant output cap),
no matter what BYOK rows or subscription tokens exist on the deploy.
"""

from __future__ import annotations

import asyncio

import pytest

from prep.agent.fake import FakeAgent
from prep.agent.openai_compat import OpenAICompatAdapter
from prep.agent.port import AgentBusy, AgentResult, AgentTimeout
from prep.agent.selector import _FREE_TIER_MAX_TOKENS, _subscription_path_allowed
from prep.infrastructure import db as infra_db
from prep.instant import service

_FREE_ENV = {
    "PREP_FREE_INFERENCE_BASE_URL": "https://inference.example/v1",
    "PREP_FREE_INFERENCE_API_KEY": "free-tier-key",
    "PREP_FREE_INFERENCE_MODEL": "test-free-model",
}


def _fake(text: str) -> FakeAgent:
    return FakeAgent(next_response=AgentResult(text=text, model="fake-model"))


def _items(n: int) -> str:
    import json

    return json.dumps(
        [{"q": f"Question {i}?", "a": f"answer {i}", "r": f"answer {i}"} for i in range(n)]
    )


@pytest.fixture
def _free_tier_env(monkeypatch: pytest.MonkeyPatch):
    for name, value in _FREE_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("PREP_FREE_INFERENCE_EXTRA_BODY", raising=False)


@pytest.fixture
def _no_free_tier(monkeypatch: pytest.MonkeyPatch):
    for name in _FREE_ENV:
        monkeypatch.delenv(name, raising=False)


# ---- topic hygiene ----------------------------------------------------------


@pytest.mark.parametrize("raw", [None, 42, [], {}, "", "   ", "\x00\x01", "x" * 501, "x" * 20000])
def test_sanitize_topic_rejects_unusable_input(raw):
    assert service.sanitize_topic(raw) is None


def test_sanitize_topic_strips_and_removes_control_chars():
    assert service.sanitize_topic("  Postgres\x00 MVCC\x07  ") == "Postgres MVCC"


def test_sanitize_topic_collapses_tabs_and_newlines_to_spaces():
    assert service.sanitize_topic("a\tb\r\nc") == "a b  c"


def test_sanitize_topic_accepts_the_cap_exactly():
    assert service.sanitize_topic("x" * 500) == "x" * 500


def test_display_name_collapses_whitespace_and_caps_length():
    assert service.display_name_for("Postgres   MVCC") == "Postgres MVCC"
    long = "word " * 30
    name = service.display_name_for(long)
    assert len(name) <= service.DISPLAY_NAME_MAX_CHARS
    assert not name.endswith(" ")


def test_build_prompt_embeds_the_topic_only_once():
    prompt = service.build_prompt("Postgres MVCC")
    assert "Topic: Postgres MVCC" in prompt


# ---- adapter resolution -------------------------------------------------------


def test_factory_seam_receives_the_default_output_cap(instant_factory, monkeypatch):
    monkeypatch.delenv("PREP_INSTANT_MAX_OUTPUT_TOKENS", raising=False)
    seen: dict = {}

    def factory(*, max_output_tokens):
        seen["cap"] = max_output_tokens
        return None

    instant_factory(factory)
    service.resolve_agent()
    assert seen["cap"] == service.DEFAULT_MAX_OUTPUT_TOKENS


def test_env_overrides_the_output_cap(instant_factory, monkeypatch):
    monkeypatch.setenv("PREP_INSTANT_MAX_OUTPUT_TOKENS", "4096")
    seen: dict = {}
    instant_factory(lambda *, max_output_tokens: seen.setdefault("cap", max_output_tokens))
    service.resolve_agent()
    assert seen["cap"] == 4096


def test_garbage_env_cap_falls_back_to_default(instant_factory, monkeypatch):
    monkeypatch.setenv("PREP_INSTANT_MAX_OUTPUT_TOKENS", "lots")
    seen: dict = {}
    instant_factory(lambda *, max_output_tokens: seen.setdefault("cap", max_output_tokens))
    service.resolve_agent()
    assert seen["cap"] == service.DEFAULT_MAX_OUTPUT_TOKENS


@pytest.mark.parametrize("raw", ["0", "-5"])
def test_nonpositive_env_cap_falls_back_to_default(instant_factory, monkeypatch, raw):
    # A non-positive cap would 4xx upstream on every call and charge
    # visitors for the failures; it must not reach the adapter.
    monkeypatch.setenv("PREP_INSTANT_MAX_OUTPUT_TOKENS", raw)
    seen: dict = {}
    instant_factory(lambda *, max_output_tokens: seen.setdefault("cap", max_output_tokens))
    service.resolve_agent()
    assert seen["cap"] == service.DEFAULT_MAX_OUTPUT_TOKENS


def test_real_factory_builds_the_capped_free_adapter(_free_tier_env, monkeypatch):
    monkeypatch.delenv("PREP_INSTANT_MAX_OUTPUT_TOKENS", raising=False)
    agent = service.resolve_agent()
    assert isinstance(agent, OpenAICompatAdapter)
    # The instant cap, never the transform-sized default.
    assert agent._max_tokens == service.DEFAULT_MAX_OUTPUT_TOKENS
    assert agent._max_tokens != _FREE_TIER_MAX_TOKENS


def test_byok_and_subscription_are_unreachable(initialized_db, _no_free_tier, monkeypatch):
    # Deploy shape where every agent_for_user path is live: tailscale
    # mode (subscription allowed), a subscription token set, and a
    # stored BYOK row. With no free tier the instant endpoint must
    # still resolve nothing.
    monkeypatch.delenv("PREP_AUTH_MODE", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test-token")
    with infra_db.cursor() as c:
        c.execute(
            "INSERT INTO byok_credentials"
            " (user_id, provider, ciphertext, key_prefix, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (initialized_db, "anthropic_api", "ciphertext", "sk-ant-api03-xx", "2026-01-01"),
        )
    assert _subscription_path_allowed()  # the shape would fund agent_for_user
    assert service.resolve_agent() is None


def test_free_tier_wins_even_when_subscription_is_configured(
    initialized_db, _free_tier_env, monkeypatch
):
    monkeypatch.delenv("PREP_AUTH_MODE", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test-token")
    agent = service.resolve_agent()
    assert isinstance(agent, OpenAICompatAdapter)


# ---- outcome mapping ------------------------------------------------------------


async def test_happy_path_returns_wire_cards(deck_json):
    fake = _fake(deck_json(5))
    deck = await service.generate(fake, "Postgres MVCC")
    assert deck.display_name == "Postgres MVCC"
    assert len(deck.cards) == 5
    assert deck.cards[0] == {
        "prompt": "Question 0?",
        "answer": "answer 0",
        "answer_regex": "answer 0",
    }
    # The adapter transport timeout backstops the service deadline.
    assert fake.calls[0]["timeout_s"] == service._ADAPTER_TIMEOUT_S


async def test_fenced_output_survives_the_shared_parser(deck_json):
    deck = await service.generate(_fake(f"```json\n{deck_json(4)}\n```"), "topic")
    assert len(deck.cards) == 4


async def test_agent_busy_maps_to_no_spend_busy():
    class _BusyAgent:
        async def run(self, prompt, *, model=None, reasoning=None, timeout_s=120.0):
            raise AgentBusy("free tier saturated")

    with pytest.raises(service.InstantBusy):
        await service.generate(_BusyAgent(), "topic")


async def test_agent_timeout_counts_as_spend():
    # The shared adapter raises AgentTimeout for its own transport
    # timeout; that call spent tokens, so it must not class as a free
    # refusal.
    class _TimeoutAgent:
        async def run(self, prompt, *, model=None, reasoning=None, timeout_s=120.0):
            raise AgentTimeout("free AI timed out")

    with pytest.raises(service.InstantGenerationFailed):
        await service.generate(_TimeoutAgent(), "topic")


async def test_agent_unavailable_counts_as_spend():
    with pytest.raises(service.InstantGenerationFailed):
        await service.generate(FakeAgent(raise_unavailable=True), "topic")


async def test_stalled_upstream_times_out_as_spend(monkeypatch):
    class _SlowAgent:
        async def run(self, prompt, *, model=None, reasoning=None, timeout_s=120.0):
            await asyncio.sleep(30)

    monkeypatch.setattr(service, "GENERATION_TIMEOUT_S", 0.05)
    with pytest.raises(service.InstantGenerationFailed, match="timed out"):
        await service.generate(_SlowAgent(), "topic")


async def test_unparseable_output_counts_as_spend():
    with pytest.raises(service.InstantGenerationFailed, match="unparseable"):
        await service.generate(_fake("here is a poem instead of JSON"), "topic")


async def test_semaphore_contention_is_busy_without_an_upstream_call():
    fake = _fake("never called")
    await service._semaphore.acquire()
    await service._semaphore.acquire()
    try:
        with pytest.raises(service.InstantBusy):
            await service.generate(fake, "topic")
    finally:
        service._semaphore.release()
        service._semaphore.release()
    assert fake.calls == []


# ---- output hygiene ---------------------------------------------------------------


async def test_three_card_floor_admits_exactly_three(deck_json):
    deck = await service.generate(_fake(deck_json(3)), "topic")
    assert len(deck.cards) == 3


async def test_fewer_than_three_cards_is_degenerate(deck_json):
    with pytest.raises(service.InstantGenerationFailed, match="degenerate"):
        await service.generate(_fake(deck_json(2)), "topic")


def test_output_is_truncated_at_the_card_cap(deck_json):
    cards = service._extract_cards(deck_json(30))
    assert len(cards) == service.MAX_CARDS


def test_invalid_regex_drops_to_null():
    import json

    text = json.dumps(
        [
            {"q": "Q1?", "a": "alpha", "r": "(unclosed"},
            {"q": "Q2?", "a": "beta", "r": "gamma"},  # does not match the answer
            {"q": "Q3?", "a": "delta", "r": "delta|d"},
        ]
    )
    cards = service._extract_cards(text)
    assert [c["answer_regex"] for c in cards] == [None, None, "delta|d"]


def test_items_missing_fields_or_over_caps_are_skipped():
    import json

    text = json.dumps(
        [
            "not a dict",
            {"q": "", "a": "x"},
            {"q": "Q?", "a": ""},
            {"q": "x" * (service.CARD_PROMPT_MAX_CHARS + 1), "a": "x"},
            {"q": "Q?", "a": "x" * (service.CARD_ANSWER_MAX_CHARS + 1)},
            {"q": "Q1?", "a": "a1"},
            {"q": "Q2?", "a": "a2"},
            {"q": "Q3?", "a": "a3", "r": 42},
        ]
    )
    cards = service._extract_cards(text)
    assert [c["prompt"] for c in cards] == ["Q1?", "Q2?", "Q3?"]
    # Non-string regex garbage is dropped to null, not an error.
    assert cards[2]["answer_regex"] is None
