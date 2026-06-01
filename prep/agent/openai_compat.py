"""prep.agent.openai_compat — shared base for OpenAI-compatible
chat-completions APIs.

OpenAI and OpenRouter both expose the same wire format:

    POST <base>/chat/completions
    Authorization: Bearer <key>
    {"model": "...", "messages": [{"role": "user", "content": "..."}]}

    →

    {"choices": [{"message": {"content": "..."}}],
     "usage": {"prompt_tokens": ..., "completion_tokens": ...}}

We collapse the common path into one base adapter; the two concrete
adapters override only the endpoint URL, the default model, and any
provider-specific headers (OpenRouter recommends HTTP-Referer +
X-Title so prep gets attributed in their dashboard).

Cost is reported as None for both: OpenAI/OpenRouter don't return a
dollar figure in the response (their dashboards compute it from
token counts × the per-model rate, which we don't mirror).
"""

from __future__ import annotations

import logging
import time

import httpx

from prep.agent.port import (
    AgentBudgetExhausted,
    AgentPort,
    AgentResult,
    AgentUnavailable,
)

logger = logging.getLogger(__name__)

_MAX_TOKENS_DEFAULT = 4096


class OpenAICompatAdapter(AgentPort):
    """Base class — concrete adapters set the four `_*` class attrs
    below. Stateless aside from the user's API key bound at __init__.
    """

    # Concrete adapters override these.
    _api_base: str = ""  # e.g. "https://api.openai.com/v1"
    _default_model: str = ""
    _prefix_check: tuple[str, ...] = ("sk-",)
    _provider_label: str = "provider"

    def __init__(self, api_key: str, *, max_tokens: int = _MAX_TOKENS_DEFAULT):
        if not api_key or not any(api_key.startswith(p) for p in self._prefix_check):
            # Belt-and-suspenders against a key shape the settings
            # route should have caught. Surfaces as AgentUnavailable
            # rather than a 401 from upstream.
            raise AgentUnavailable(f"invalid {self._provider_label} API key shape")
        self._api_key = api_key
        self._max_tokens = max_tokens

    def _extra_headers(self) -> dict[str, str]:
        """Hook for provider-specific headers (e.g. OpenRouter's
        attribution headers). Defaults to empty."""
        return {}

    async def run(
        self,
        prompt: str,
        *,
        model: str | None = None,
        reasoning: str | None = None,  # noqa: ARG002 — not exposed by chat-completions
        timeout_s: float = 120.0,
    ) -> AgentResult:
        chosen_model = model or self._default_model
        body = {
            "model": chosen_model,
            "max_tokens": self._max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            **self._extra_headers(),
        }

        url = f"{self._api_base}/chat/completions"
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.post(url, json=body, headers=headers)
        except httpx.TimeoutException as e:
            raise AgentUnavailable(f"{self._provider_label} API timeout after {timeout_s}s") from e
        except httpx.HTTPError as e:
            raise AgentUnavailable(f"{self._provider_label} API transport error: {e}") from e
        duration_ms = int((time.monotonic() - started) * 1000)

        if resp.status_code != 200:
            self._raise_for_error_response(resp)

        try:
            data = resp.json()
        except ValueError as e:
            raise AgentUnavailable(f"{self._provider_label} API returned non-JSON body: {e}") from e

        try:
            choices = data.get("choices") or []
            message = (choices[0].get("message") if choices else {}) or {}
            text = (message.get("content") or "").strip()
        except (AttributeError, IndexError) as e:
            raise AgentUnavailable(
                f"{self._provider_label} API response shape unexpected: {e}"
            ) from e

        if not text:
            raise AgentUnavailable(f"{self._provider_label} API returned no text content")

        usage = data.get("usage") or {}
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")

        return AgentResult(
            text=text,
            model=chosen_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=None,  # provider dashboards compute this; we don't mirror it
            duration_ms=duration_ms,
        )

    def _raise_for_error_response(self, resp: httpx.Response) -> None:
        """Translate an OpenAI-compatible error response into the right
        AgentUnavailable subclass. We cap the error message length so
        a verbose upstream body doesn't bloat log lines."""
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        error = (payload or {}).get("error") or {}
        msg = str(error.get("message") or f"HTTP {resp.status_code}").strip()[:200]
        err_type = (error.get("type") or "").strip().lower()
        err_code = (error.get("code") or "").strip().lower()

        if resp.status_code in (401, 403):
            raise AgentUnavailable(f"{self._provider_label} API auth rejected: {msg}")
        # OpenAI maps quota/billing to 429 with code `insufficient_quota`
        # or type `insufficient_quota`; OpenRouter does similar.
        if resp.status_code == 429 or "quota" in err_type or "quota" in err_code:
            raise AgentBudgetExhausted(f"{self._provider_label} API quota/rate exhausted: {msg}")
        raise AgentUnavailable(f"{self._provider_label} API error ({resp.status_code}): {msg}")
