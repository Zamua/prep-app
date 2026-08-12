"""Recorded-shape contract tests for the free-tier output contract.

Fixtures under tests/agent/fixtures/openai_compat/ are
chat-completions response bodies named by shape (docs/AI-PROVIDERS.md
section 3). Two layers consume them:

- the adapter (transport-injected, no sockets): each shape either
  returns text or raises the pinned AgentPort exception
- the caller-side parse helpers (`_parse_qa_pairs`,
  `_parse_grade_json`, `validate_regex_update`) and the `ai_grade`
  consumer: content-bearing shapes parse, damaged shapes hit the
  documented fallback (raise / string-match), never silent bad data

The Go worker's table tests
(worker-go/activities/openai_fixture_parse_test.go) consume the same
files, so the two languages stay pinned to identical shapes.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

import prep.trivia.service as svc
from prep.agent.openai_compat import OpenAICompatAdapter
from prep.agent.port import AgentBudgetExhausted, AgentBusy, AgentUnavailable
from prep.domain.grading import validate_regex_update
from prep.trivia.service import _parse_grade_json, _parse_qa_pairs

FIXTURES = Path(__file__).parent / "fixtures" / "openai_compat"

# HTTP status each fixture was recorded at; everything else is a 200.
_STATUS = {
    "rate_limited_429.json": 429,
    "auth_401.json": 401,
}

_CONTENT_SHAPES = [
    "happy.json",
    "fenced.json",
    "preamble.json",
    "think_tag.json",
    "truncated.json",
]


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _content(name: str) -> str:
    return _fixture(name)["choices"][0]["message"]["content"]


def _adapter_for(name: str, *, shared: bool = True) -> OpenAICompatAdapter:
    body = _fixture(name)
    status = _STATUS.get(name, 200)

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(status, json=body)

    return OpenAICompatAdapter(
        "fixture-key",
        base_url="https://inference.example/v1",
        model="test-model",
        shared=shared,
        transport=httpx.MockTransport(handler),
    )


def _run(adapter: OpenAICompatAdapter):
    return asyncio.run(adapter.run("prompt"))


# ---- adapter layer: each shape parses or raises the pinned exception -----


@pytest.mark.parametrize("name", _CONTENT_SHAPES)
def test_content_shapes_pass_through_the_adapter_verbatim(name: str):
    """The adapter is dumb text transport: content-bearing shapes come
    back as-is (stripped); tolerance lives in the caller-side parsers."""
    result = _run(_adapter_for(name))
    assert result.text == _content(name).strip()
    usage = _fixture(name)["usage"]
    assert result.input_tokens == usage["prompt_tokens"]
    assert result.output_tokens == usage["completion_tokens"]


def test_rate_limited_shape_shared_mode_is_busy():
    with pytest.raises(AgentBusy):
        _run(_adapter_for("rate_limited_429.json", shared=True))


def test_rate_limited_shape_byok_mode_is_budget_exhausted():
    with pytest.raises(AgentBudgetExhausted):
        _run(_adapter_for("rate_limited_429.json", shared=False))


@pytest.mark.parametrize("shared", [False, True])
def test_auth_shape_is_plain_unavailable(shared: bool):
    with pytest.raises(AgentUnavailable) as ei:
        _run(_adapter_for("auth_401.json", shared=shared))
    assert not isinstance(ei.value, (AgentBudgetExhausted, AgentBusy))


def test_empty_content_shape_is_unavailable():
    with pytest.raises(AgentUnavailable):
        _run(_adapter_for("empty_content.json"))


# ---- generation parser: _parse_qa_pairs ----------------------------------


def test_happy_shape_parses_qa_pairs():
    pairs = _parse_qa_pairs(_content("happy.json"))
    assert len(pairs) == 3
    assert all(p.get("q") and p.get("a") for p in pairs)
    assert pairs[1]["a"] == "write-ahead log"


def test_preamble_shape_parses_qa_pairs():
    """A chatty preamble before the array is tolerated (first-bracket
    heuristic)."""
    pairs = _parse_qa_pairs(_content("preamble.json"))
    assert len(pairs) == 2
    assert pairs[0]["a"] == "Pacific"


def test_truncated_shape_raises_never_partial_cards():
    """Output cut by the token cap must raise (generation surfaces an
    error), never yield a partial batch of cards."""
    with pytest.raises(ValueError):
        _parse_qa_pairs(_content("truncated.json"))


# ---- grading parser: _parse_grade_json + validate_regex_update -----------


def test_fenced_shape_parses_grade_json_and_regex_validates():
    parsed = _parse_grade_json(_content("fenced.json"))
    assert parsed["verdict"] == "right"
    accepted = validate_regex_update(
        parsed["regex_update"], expected_literal="write-ahead log", prior_given="wal"
    )
    assert accepted == parsed["regex_update"]


def test_think_tag_shape_still_parses_grade_json():
    """A leading reasoning block without braces does not defeat the
    first-brace heuristic; the grade object is still extracted."""
    parsed = _parse_grade_json(_content("think_tag.json"))
    assert parsed["verdict"] == "right"
    assert parsed["regex_update"]


def test_truncated_shape_grade_json_raises():
    with pytest.raises(ValueError):
        _parse_grade_json(_content("truncated.json"))


# ---- ai_grade consumer: pollution copes, damage falls back ---------------


async def test_ai_grade_survives_think_tag_pollution(monkeypatch):
    async def canned(*_a, **_k):
        return _content("think_tag.json")

    monkeypatch.setattr(svc, "run_prompt_async", canned)
    out = await svc.ai_grade(
        prompt="What does WAL stand for?", expected="write-ahead log", given="wal"
    )
    assert out["correct"] is True
    assert out["regex_update"] == "(write[- ]?ahead log|wal)"


async def test_ai_grade_falls_back_on_truncated_output(monkeypatch):
    """Malformed model output → deterministic string match with the
    neutral fallback feedback, regex_update always None."""

    async def canned(*_a, **_k):
        return _content("truncated.json")

    monkeypatch.setattr(svc, "run_prompt_async", canned)
    out = await svc.ai_grade(prompt="Capital of Japan?", expected="Tokyo", given="Tokyo")
    assert out["correct"] is True  # the string match, not the model
    assert "string similarity" in out["feedback"]
    assert out["regex_update"] is None
