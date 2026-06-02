"""prep.agent.sdk_adapter — `AgentPort` implementation backed by the
official `claude-agent-sdk` Python package.

This is the production adapter. It speaks to Anthropic's API directly
(no `claude` CLI binary needed) and authenticates via the
`CLAUDE_CODE_OAUTH_TOKEN` env var produced by `claude setup-token` —
which means invocations draw from the user's monthly Max
subscription credit pool (announced June 15, 2026), not a separate
API-key billing account.

The adapter is intentionally narrow: we use the SDK only as a
one-shot text-in/text-out call. We collect every `TextBlock` from
the streamed `AssistantMessage`s, capture the final `ResultMessage`
for cost + token accounting, and return one `AgentResult`. No tools,
no MCP, no session persistence — those would require widening the
port and are deliberate non-goals for the current callers.
"""

from __future__ import annotations

import logging
import os

from prep.agent.port import (
    DEFAULT_MODEL,
    DEFAULT_REASONING,
    AgentBudgetExhausted,
    AgentPort,
    AgentResult,
    AgentUnavailable,
)

# Substrings the SDK / Anthropic API surface in their error messages
# when the user has burned through their monthly agent-SDK credit
# allocation. Match is case-insensitive. We map the bare text rather
# than a typed exception because the SDK doesn't (today) expose a
# dedicated exception class for this — credit exhaustion comes back
# as a generic API error with a recognizable message.
_BUDGET_EXHAUSTED_MARKERS = (
    "credit balance",
    "monthly credit",
    "credit_balance",
    "insufficient_quota",
    "agent_sdk_credit",
    "subscription credit",
)


def _is_budget_exhausted(err_text: str) -> bool:
    low = err_text.lower()
    return any(m in low for m in _BUDGET_EXHAUSTED_MARKERS)


logger = logging.getLogger(__name__)


class ClaudeAgentSdkAdapter(AgentPort):
    """`AgentPort` implementation using `claude_agent_sdk.query`.

    Two construction modes:
      - `ClaudeAgentSdkAdapter()` — no bound token; relies on
        `CLAUDE_CODE_OAUTH_TOKEN` in the process env. This is the
        deploy-wide path used by single-user local installs.
      - `ClaudeAgentSdkAdapter(token=...)` — explicit per-user token,
        injected into the SDK subprocess via `ClaudeAgentOptions.env`
        without mutating os.environ. Concurrency-safe across users
        sharing the process; this is what the per-user subscription
        BYOK provider uses on multi-user deploys.
    """

    def __init__(self, token: str | None = None):
        self._token: str | None = (token or "").strip() or None

    async def run(
        self,
        prompt: str,
        *,
        model: str | None = None,
        reasoning: str | None = None,
        timeout_s: float = 120.0,
    ) -> AgentResult:
        # Late import: the SDK pulls in its own deps + does some
        # initialization at import time. Keep it out of the
        # module-load path so prep boots even if the package isn't
        # installed yet (during local dev / partial deploys).
        try:
            from claude_agent_sdk import (
                AssistantMessage,
                ClaudeAgentOptions,
                ResultMessage,
                TextBlock,
                query,
            )
        except ImportError as e:
            raise AgentUnavailable(
                "claude-agent-sdk not installed; run `uv sync` to pull it"
            ) from e

        # Resolve effective token: bound (per-user BYOK) wins over
        # process env (deploy-wide single-user path).
        effective_token = self._token or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        if not effective_token:
            raise AgentUnavailable(
                "CLAUDE_CODE_OAUTH_TOKEN not set — run `claude setup-token` and paste"
            )

        chosen_model = model or DEFAULT_MODEL
        chosen_reasoning = reasoning or DEFAULT_REASONING
        # SDK calls it `effort`, not `reasoning` (low | medium | high).
        # When a per-user token is bound, pass it via the subprocess
        # env dict — that scope is per-call (one SDK subprocess per
        # query) so concurrent users with different tokens never
        # race. The session-resume code (sdk/internal/session_resume.py)
        # consults `options.env` before falling back to os.environ.
        sdk_env = {"CLAUDE_CODE_OAUTH_TOKEN": effective_token} if self._token else {}
        options = ClaudeAgentOptions(model=chosen_model, effort=chosen_reasoning, env=sdk_env)

        text_parts: list[str] = []
        result_msg = None

        try:
            async for msg in query(prompt=prompt, options=options):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            text_parts.append(block.text)
                elif isinstance(msg, ResultMessage):
                    result_msg = msg
        except Exception as e:  # noqa: BLE001 — funnel any SDK fault as Unavailable
            err = str(e)
            if _is_budget_exhausted(err):
                raise AgentBudgetExhausted(
                    "monthly Claude agent-SDK allocation exhausted: " + err
                ) from e
            raise AgentUnavailable(f"claude-agent-sdk error: {e}") from e

        # Some SDK error paths come back inside a ResultMessage with
        # is_error=True instead of raising. Map those too.
        if result_msg is not None and getattr(result_msg, "is_error", False):
            err = (getattr(result_msg, "result", None) or "").strip() or "agent reported error"
            if _is_budget_exhausted(err):
                raise AgentBudgetExhausted("monthly Claude agent-SDK allocation exhausted: " + err)
            raise AgentUnavailable(f"claude-agent-sdk error: {err}")

        text = "".join(text_parts).strip()
        if not text:
            raise AgentUnavailable("claude-agent-sdk returned no text")

        # Pull cost + tokens from the final ResultMessage when present.
        # The SDK doesn't always send one (older releases / certain
        # error paths) — degrade to None rather than fail the call.
        input_tokens = output_tokens = duration_ms = cost_usd = None
        if result_msg is not None:
            cost_usd = result_msg.total_cost_usd
            duration_ms = result_msg.duration_ms
            usage = result_msg.usage or {}
            # Anthropic API usage shape — extract defensively because
            # the dict is provider-internal.
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")

        return AgentResult(
            text=text,
            model=chosen_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
        )
