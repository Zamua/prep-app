"""prep.agent.openai_compat - generic adapter for OpenAI-compatible
chat-completions APIs.

The wire format is the de-facto standard:

    POST <base>/chat/completions
    Authorization: Bearer <key>
    {"model": "...", "messages": [{"role": "user", "content": "..."}]}

    →

    {"choices": [{"message": {"content": "..."}}],
     "usage": {"prompt_tokens": ..., "completion_tokens": ...}}

Directly constructible: a vendor is a set of constructor arguments
(base_url, model, extra_body, ...). The BYOK subclasses (OpenAI,
OpenRouter) pre-bake their endpoint / default model / key-prefix
check as class attrs, which the constructor falls back to.

`shared=True` marks a deploy-wide credential (the free tier): 429s,
quota-coded error bodies, and timeouts then map to `AgentBusy`
(shared-capacity contention, not the user's budget) instead of
`AgentBudgetExhausted` / `AgentUnavailable`.

Cost is reported as None: these APIs don't return a dollar figure in
the response (dashboards compute it from token counts × per-model
rates, which we don't mirror).
"""

from __future__ import annotations

import logging
import time

import httpx

from prep.agent.port import (
    AgentBudgetExhausted,
    AgentBusy,
    AgentPort,
    AgentResult,
    AgentUnavailable,
)

logger = logging.getLogger(__name__)

_MAX_TOKENS_DEFAULT = 4096


class OpenAICompatAdapter(AgentPort):
    """Generic OpenAI-compatible chat-completions adapter.

    Stateless aside from the configuration bound at __init__.
    Subclasses may override the `_*` class attrs as defaults;
    constructor args win over them.
    """

    # Defaults for subclasses; constructor args override.
    _api_base: str = ""  # e.g. "https://api.openai.com/v1"
    _default_model: str = ""
    # Accepted key prefixes. An EMPTY tuple skips shape validation
    # entirely (the key shape is not ours to police - free-tier /
    # arbitrary endpoints mint keys in shapes we don't know); a
    # non-empty tuple rejects keys matching none of the prefixes.
    _prefix_check: tuple[str, ...] = ()
    _provider_label: str = "AI"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        model: str | None = None,
        extra_body: dict | None = None,
        shared: bool = False,
        max_tokens: int = _MAX_TOKENS_DEFAULT,
        provider_label: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if provider_label is not None:
            self._provider_label = provider_label
        if not api_key or (
            self._prefix_check and not any(api_key.startswith(p) for p in self._prefix_check)
        ):
            # Belt-and-suspenders against a key shape the settings
            # route should have caught. Surfaces as AgentUnavailable
            # rather than a 401 from upstream.
            raise AgentUnavailable(f"invalid {self._provider_label} API key shape")
        self._api_key = api_key
        self._base_url = (base_url or self._api_base).rstrip("/")
        self._model = model or self._default_model
        self._extra_body = dict(extra_body) if extra_body else {}
        if "messages" in self._extra_body:
            # extra_body carries deploy knobs, never the conversation.
            logger.warning(
                "%s adapter: extra_body may not override 'messages'; dropping the key",
                self._provider_label,
            )
            self._extra_body.pop("messages")
        self._shared = shared
        self._max_tokens = max_tokens
        self._transport = transport

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
        # Merge order: adapter defaults < extra_body (deploy knobs) <
        # caller args. `messages` is never overridable.
        body: dict = {
            "model": self._model,
            "max_tokens": self._max_tokens,
        }
        body.update(self._extra_body)
        if model:
            body["model"] = model
        body["messages"] = [{"role": "user", "content": prompt}]
        chosen_model = body["model"]
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            **self._extra_headers(),
        }

        url = f"{self._base_url}/chat/completions"
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout_s, transport=self._transport) as client:
                resp = await client.post(url, json=body, headers=headers)
        except httpx.TimeoutException as e:
            if self._shared:
                # Shared credential: a timeout is capacity contention
                # from the caller's perspective, not their fault. The
                # message is user-visible (workflow error surfaces
                # render it verbatim), so it carries the remedy.
                raise AgentBusy(
                    f"{self._provider_label} timed out after {timeout_s}s; "
                    "try again later, or add your own key in Settings for "
                    "dedicated capacity"
                ) from e
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

        if isinstance(data, dict) and data.get("error"):
            # Some endpoints put an error object - including quota-coded
            # ones - in a 200 body. Route it through the same mapping as
            # non-200 responses so the mode split (busy vs budget) holds
            # at any status.
            self._raise_for_error_payload(resp.status_code, data)

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
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        self._raise_for_error_payload(resp.status_code, payload)

    def _raise_for_error_payload(self, status_code: int, payload: dict) -> None:
        """Translate an OpenAI-compatible error payload into the right
        AgentUnavailable subclass. We cap the error message length so
        a verbose upstream body doesn't bloat log lines.

        Mode split: with a user's own key (shared=False), 429s and
        quota-coded bodies mean THEIR budget → AgentBudgetExhausted.
        With a deploy-wide shared key (shared=True), the same signals
        mean shared-capacity contention → AgentBusy."""
        error = (payload or {}).get("error") or {}
        if not isinstance(error, dict):
            # Some endpoints send `error` as a bare string; the mapping
            # must stay inside the AgentPort taxonomy either way.
            error = {"message": str(error)}
        msg = str(error.get("message") or f"HTTP {status_code}").strip()[:200]
        err_type = str(error.get("type") or "").strip().lower()
        err_code = str(error.get("code") or "").strip().lower()

        if status_code in (401, 403):
            if self._shared:
                # The deploy-wide key is bad - operator misconfig, not
                # a user problem. Loud so the operator sees it.
                logger.error(
                    "%s API rejected the deploy-wide shared key (HTTP %s): %s",
                    self._provider_label,
                    status_code,
                    msg,
                )
            raise AgentUnavailable(f"{self._provider_label} API auth rejected: {msg}")
        # OpenAI maps quota/billing to 429 with code `insufficient_quota`
        # or type `insufficient_quota`; OpenRouter does similar. Some
        # endpoints put a quota-coded error body on other statuses.
        if status_code == 429 or "quota" in err_type or "quota" in err_code:
            if self._shared:
                # User-visible via workflow error surfaces; no promised
                # recovery time (the upstream 429 covers both
                # minute-scale limits and the daily cap).
                raise AgentBusy(
                    f"{self._provider_label} is busy right now (it's shared by everyone "
                    f"on this deploy): {msg}; try again later, or add your own key in "
                    "Settings for dedicated capacity"
                )
            raise AgentBudgetExhausted(f"{self._provider_label} API quota/rate exhausted: {msg}")
        raise AgentUnavailable(f"{self._provider_label} API error ({status_code}): {msg}")
