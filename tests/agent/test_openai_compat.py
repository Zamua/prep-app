"""Contract tests for the generic OpenAI-compatible adapter.

Everything runs against `httpx.MockTransport` via the adapter's
`transport=` constructor arg - no sockets, no real providers. Pins:
the constructor contract (base_url / model / extra_body / shared),
the prefix-check semantics (empty tuple = skip validation), the
request shape, and the two-mode error mapping table from
docs/AI-PROVIDERS.md section 2.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from prep.agent.openai_api import OpenAIAdapter
from prep.agent.openai_compat import OpenAICompatAdapter
from prep.agent.openrouter import OpenRouterAdapter
from prep.agent.port import AgentBudgetExhausted, AgentBusy, AgentUnavailable

_BASE = "https://inference.example/v1"


def _ok_payload(text: str = "hello", prompt_tokens: int = 11, completion_tokens: int = 7) -> dict:
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


def _capture_transport(captured: list, payload: dict | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=payload if payload is not None else _ok_payload())

    return httpx.MockTransport(handler)


def _error_transport(status: int, body: dict | str | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body if body is not None else {})

    return httpx.MockTransport(handler)


def _timeout_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        raise httpx.ReadTimeout("simulated timeout")

    return httpx.MockTransport(handler)


def _run(adapter: OpenAICompatAdapter, prompt: str = "hi", **kwargs):
    return asyncio.run(adapter.run(prompt, **kwargs))


# ---- constructor: prefix-check semantics ---------------------------------


def test_empty_prefix_tuple_accepts_any_key_shape():
    """The generic adapter's key shape is not ours to validate - an
    empty prefix tuple SKIPS validation (it must not reject
    everything, which is what any() over an empty tuple would do)."""
    adapter = OpenAICompatAdapter("weird-key-shape-123", base_url=_BASE, model="m")
    assert isinstance(adapter, OpenAICompatAdapter)


def test_empty_key_rejected_even_without_prefix_check():
    with pytest.raises(AgentUnavailable):
        OpenAICompatAdapter("", base_url=_BASE, model="m")


def test_byok_subclasses_still_reject_mismatched_prefixes():
    with pytest.raises(AgentUnavailable):
        OpenAIAdapter("not-an-openai-key")
    with pytest.raises(AgentUnavailable):
        OpenRouterAdapter("sk-but-not-openrouter")


def test_byok_subclasses_still_accept_their_prefixes():
    assert isinstance(OpenAIAdapter("sk-proj-abc"), OpenAIAdapter)
    assert isinstance(OpenRouterAdapter("sk-or-v1-abc"), OpenRouterAdapter)


# ---- request shape -------------------------------------------------------


def test_request_shape_and_happy_parse():
    captured: list[httpx.Request] = []
    adapter = OpenAICompatAdapter(
        "free-key",
        base_url=_BASE,
        model="test-model",
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        shared=True,
        transport=_capture_transport(captured),
    )
    result = _run(adapter, "say hi")

    assert len(captured) == 1
    req = captured[0]
    assert str(req.url) == f"{_BASE}/chat/completions"
    assert req.headers["Authorization"] == "Bearer free-key"
    body = json.loads(req.content)
    assert body["model"] == "test-model"
    assert body["max_tokens"] == 4096
    assert body["messages"] == [{"role": "user", "content": "say hi"}]
    # extra_body merged into the request body.
    assert body["chat_template_kwargs"] == {"enable_thinking": False}

    assert result.text == "hello"
    assert result.model == "test-model"
    assert result.input_tokens == 11
    assert result.output_tokens == 7
    assert result.cost_usd is None


def test_extra_body_cannot_override_messages():
    captured: list[httpx.Request] = []
    adapter = OpenAICompatAdapter(
        "k",
        base_url=_BASE,
        model="m",
        extra_body={"messages": [{"role": "system", "content": "evil"}], "temperature": 0},
        transport=_capture_transport(captured),
    )
    _run(adapter, "real prompt")
    body = json.loads(captured[0].content)
    assert body["messages"] == [{"role": "user", "content": "real prompt"}]
    assert body["temperature"] == 0


def test_merge_precedence_caller_beats_extra_body_beats_defaults():
    """extra_body wins over the adapter's configured defaults; the
    caller's per-run args win over extra_body."""
    captured: list[httpx.Request] = []
    adapter = OpenAICompatAdapter(
        "k",
        base_url=_BASE,
        model="constructor-model",
        extra_body={"model": "extra-body-model", "max_tokens": 128},
        transport=_capture_transport(captured),
    )
    _run(adapter)
    body = json.loads(captured[0].content)
    assert body["model"] == "extra-body-model"
    assert body["max_tokens"] == 128

    result = _run(adapter, model="caller-model")
    body = json.loads(captured[1].content)
    assert body["model"] == "caller-model"
    assert result.model == "caller-model"


def test_max_tokens_constructor_arg_reaches_the_wire():
    """The transform path needs an output cap well above 4096; the
    constructor arg is how the factory raises it."""
    captured: list[httpx.Request] = []
    adapter = OpenAICompatAdapter(
        "k", base_url=_BASE, model="m", max_tokens=32768, transport=_capture_transport(captured)
    )
    _run(adapter)
    assert json.loads(captured[0].content)["max_tokens"] == 32768


def test_base_url_trailing_slash_normalized():
    captured: list[httpx.Request] = []
    adapter = OpenAICompatAdapter(
        "k", base_url=_BASE + "/", model="m", transport=_capture_transport(captured)
    )
    _run(adapter)
    assert str(captured[0].url) == f"{_BASE}/chat/completions"


def test_byok_subclass_defaults_and_headers_preserved():
    """OpenAIAdapter / OpenRouterAdapter keep their class-attr
    endpoint + default model + provider headers when constructed the
    pre-M2 way (api_key only)."""
    captured: list[httpx.Request] = []
    _run(OpenAIAdapter("sk-abc", transport=_capture_transport(captured)))
    assert str(captured[0].url) == "https://api.openai.com/v1/chat/completions"
    assert json.loads(captured[0].content)["model"] == "gpt-5-mini"

    captured.clear()
    _run(OpenRouterAdapter("sk-or-v1-abc", transport=_capture_transport(captured)))
    assert str(captured[0].url) == "https://openrouter.ai/api/v1/chat/completions"
    assert json.loads(captured[0].content)["model"] == "anthropic/claude-sonnet-4.5"
    assert captured[0].headers["X-Title"] == "prep"


# ---- error mapping: the two-mode table -----------------------------------


def _adapter(transport: httpx.MockTransport, *, shared: bool) -> OpenAICompatAdapter:
    return OpenAICompatAdapter("k", base_url=_BASE, model="m", shared=shared, transport=transport)


def _assert_plain_unavailable(exc: AgentUnavailable):
    """Bare AgentUnavailable, not one of the specific subclasses."""
    assert not isinstance(exc, (AgentBudgetExhausted, AgentBusy))


def test_429_byok_mode_is_budget_exhausted():
    with pytest.raises(AgentBudgetExhausted):
        _run(_adapter(_error_transport(429, {"error": {"message": "rate limited"}}), shared=False))


def test_429_shared_mode_is_busy():
    with pytest.raises(AgentBusy):
        _run(_adapter(_error_transport(429, {"error": {"message": "rate limited"}}), shared=True))


def test_quota_coded_body_at_other_status_byok_mode():
    body = {"error": {"message": "no credit", "type": "insufficient_quota"}}
    with pytest.raises(AgentBudgetExhausted):
        _run(_adapter(_error_transport(400, body), shared=False))


def test_quota_coded_body_at_other_status_shared_mode():
    body = {"error": {"message": "no credit", "code": "insufficient_quota"}}
    with pytest.raises(AgentBusy):
        _run(_adapter(_error_transport(400, body), shared=True))


def test_quota_coded_body_at_200_byok_mode():
    """The mapping table says "a quota-coded error body at any status" -
    including a 200 whose body carries an error object instead of
    choices."""
    body = {"error": {"message": "no credit", "type": "insufficient_quota"}}
    with pytest.raises(AgentBudgetExhausted):
        _run(_adapter(_error_transport(200, body), shared=False))


def test_quota_coded_body_at_200_shared_mode():
    body = {"error": {"message": "daily cap reached", "code": "insufficient_quota"}}
    with pytest.raises(AgentBusy):
        _run(_adapter(_error_transport(200, body), shared=True))


def test_string_error_field_stays_inside_taxonomy():
    """Some endpoints send `error` as a bare string; the mapping must
    still raise from the AgentPort taxonomy, never a raw AttributeError."""
    with pytest.raises(AgentBudgetExhausted):
        _run(_adapter(_error_transport(429, {"error": "slow down"}), shared=False))
    with pytest.raises(AgentUnavailable) as ei:
        _run(_adapter(_error_transport(502, {"error": "upstream sad"}), shared=False))
    _assert_plain_unavailable(ei.value)
    assert "upstream sad" in str(ei.value)


def test_timeout_byok_mode_is_plain_unavailable():
    with pytest.raises(AgentUnavailable) as ei:
        _run(_adapter(_timeout_transport(), shared=False))
    _assert_plain_unavailable(ei.value)


def test_timeout_shared_mode_is_busy():
    with pytest.raises(AgentBusy):
        _run(_adapter(_timeout_transport(), shared=True))


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("shared", [False, True])
def test_auth_rejection_is_plain_unavailable_both_modes(status: int, shared: bool):
    with pytest.raises(AgentUnavailable) as ei:
        _run(_adapter(_error_transport(status, {"error": {"message": "bad key"}}), shared=shared))
    _assert_plain_unavailable(ei.value)


@pytest.mark.parametrize("shared", [False, True])
def test_other_5xx_is_plain_unavailable(shared: bool):
    with pytest.raises(AgentUnavailable) as ei:
        _run(_adapter(_error_transport(500, {"error": {"message": "boom"}}), shared=shared))
    _assert_plain_unavailable(ei.value)


# ---- degenerate 200 bodies -----------------------------------------------


def test_non_json_200_body_is_unavailable():
    with pytest.raises(AgentUnavailable):
        _run(_adapter(_error_transport(200, "<html>not json</html>"), shared=True))


def test_empty_choices_is_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, json={"choices": []})

    with pytest.raises(AgentUnavailable):
        _run(_adapter(httpx.MockTransport(handler), shared=True))


def test_empty_content_is_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, json={"choices": [{"message": {"content": "   "}}]})

    with pytest.raises(AgentUnavailable):
        _run(_adapter(httpx.MockTransport(handler), shared=True))


# ---- exception taxonomy --------------------------------------------------


def test_busy_is_an_unavailable_subclass():
    """Existing catch-sites (except AgentUnavailable) must keep
    swallowing AgentBusy; only busy-aware callers catch it first."""
    assert issubclass(AgentBusy, AgentUnavailable)
    assert not issubclass(AgentBusy, AgentBudgetExhausted)
