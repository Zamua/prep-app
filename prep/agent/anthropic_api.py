"""prep.agent.anthropic_api — `AgentPort` impl over Anthropic's
Messages API with a user-supplied API key.

Sibling to `sdk_adapter.py`. Differences:
  - Authenticates via `x-api-key` (user's `sk-ant-api03-…` key)
    rather than `CLAUDE_CODE_OAUTH_TOKEN` (user's subscription
    OAuth token from `claude setup-token`).
  - Talks directly to `https://api.anthropic.com/v1/messages` via
    `httpx` — no `claude-agent-sdk` import. Smaller surface; doesn't
    pull in mcp / anyio init machinery just to fire off one call.
  - Stateless per call; the API key is bound at construction so each
    user's adapter is its own instance. We do NOT cache adapters
    across users — the selector creates one per (user_id) on demand,
    which is fine because the adapter is just a thin httpx wrapper.

Why we built our own rather than depend on the `anthropic` Python
SDK: a single endpoint, a stable JSON contract, and `httpx` is
already in the dep tree (clerk-backend-api). Adding another SDK +
its version constraints is more risk than benefit for one POST.

## Cost reporting

The API doesn't return a dollar figure — only token counts. Users
are billed by Anthropic directly per their account's pricing, so
we report `input_tokens` / `output_tokens` and leave `cost_usd` at
None for this adapter. (`ClaudeAgentSdkAdapter` gets a cost figure
from the SDK because subscription-credit usage IS dollar-tracked
on Anthropic's side.)
"""

from __future__ import annotations

import logging
import time

import httpx

from prep.agent.port import (
    DEFAULT_MODEL,
    AgentBudgetExhausted,
    AgentPort,
    AgentResult,
    AgentUnavailable,
)

logger = logging.getLogger(__name__)

_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"
# A safety upper bound on the response size we'll request. Most prep
# AI calls produce a few hundred tokens; bumping this is harmless.
_MAX_TOKENS_DEFAULT = 4096


# Anthropic surfaces credit / quota issues as 400/429 with an `error`
# block whose `type` is one of these. Map them to AgentBudgetExhausted
# so the UI can render a specific "your Anthropic balance is exhausted"
# message instead of a generic agent failure.
_BUDGET_ERROR_TYPES = frozenset(
    {
        "rate_limit_error",
        "credit_balance_too_low",
        "billing_error",
    }
)


class AnthropicApiAdapter(AgentPort):
    """`AgentPort` impl that calls api.anthropic.com with a BYOK key.

    Construct with the user's plaintext API key. The adapter holds it
    for the lifetime of the instance — keep instances request-scoped
    so the key doesn't outlive the request that needed it.

    `max_tokens` here is the API's max_tokens (cap on response
    length), not a budget. The prep callers don't currently tune it,
    so we default to a generous value.
    """

    def __init__(self, api_key: str, *, max_tokens: int = _MAX_TOKENS_DEFAULT):
        if not api_key or not api_key.startswith("sk-ant-"):
            # Belt-and-suspenders — the settings route validates the
            # prefix on store, but the adapter's invariant is that
            # whatever it sends to Anthropic looks like an API key.
            raise AgentUnavailable("invalid Anthropic API key shape")
        self._api_key = api_key
        self._max_tokens = max_tokens

    async def run(
        self,
        prompt: str,
        *,
        model: str | None = None,
        reasoning: str | None = None,  # noqa: ARG002 — unused; Messages API
        # doesn't expose a `reasoning` knob, the SDK does. Kept on the
        # signature for AgentPort conformance.
        timeout_s: float = 120.0,
    ) -> AgentResult:
        chosen_model = model or DEFAULT_MODEL
        body = {
            "model": chosen_model,
            "max_tokens": self._max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }

        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.post(_API_URL, json=body, headers=headers)
        except httpx.TimeoutException as e:
            raise AgentUnavailable(f"anthropic API timeout after {timeout_s}s") from e
        except httpx.HTTPError as e:
            raise AgentUnavailable(f"anthropic API transport error: {e}") from e
        duration_ms = int((time.monotonic() - started) * 1000)

        if resp.status_code != 200:
            self._raise_for_error_response(resp)

        try:
            data = resp.json()
        except ValueError as e:
            raise AgentUnavailable(f"anthropic API returned non-JSON body: {e}") from e

        # Extract the joined text content. The Messages API returns a
        # list of content blocks; we keep only the `text` ones (this
        # adapter doesn't request tool use, so that's all we'll get).
        try:
            blocks = data.get("content") or []
            text_parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
        except AttributeError as e:
            raise AgentUnavailable(f"anthropic API response shape unexpected: {e}") from e

        text = "".join(text_parts).strip()
        if not text:
            raise AgentUnavailable("anthropic API returned no text content")

        usage = data.get("usage") or {}
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")

        return AgentResult(
            text=text,
            model=chosen_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            # Cost not provided by the API for BYOK keys — Anthropic
            # bills the user's account directly. See module docstring.
            cost_usd=None,
            duration_ms=duration_ms,
        )

    # ---- helpers ---------------------------------------------------------

    @staticmethod
    def _raise_for_error_response(resp: httpx.Response) -> None:
        """Translate an Anthropic API error response into the right
        AgentUnavailable subclass. We never leak the response body
        into the exception message verbatim — Anthropic sometimes
        echoes parts of the request, which could include the prompt.
        """
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        error = (payload or {}).get("error") or {}
        err_type = (error.get("type") or "").strip().lower()
        # The human-readable message is generally safe (Anthropic
        # error messages are short + don't echo user data), but cap
        # length to keep log lines bounded.
        msg = (error.get("message") or f"HTTP {resp.status_code}").strip()[:200]

        # Auth issues are always "configure your key" UX — surface
        # them as a clear AgentUnavailable.
        if resp.status_code in (401, 403):
            raise AgentUnavailable(f"anthropic API auth rejected: {msg}")
        if err_type in _BUDGET_ERROR_TYPES or resp.status_code == 429:
            raise AgentBudgetExhausted(f"anthropic API budget/rate exhausted: {msg}")
        raise AgentUnavailable(f"anthropic API error ({resp.status_code}): {msg}")
