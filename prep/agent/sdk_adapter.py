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
    AgentPort,
    AgentResult,
    AgentUnavailable,
)

logger = logging.getLogger(__name__)


class ClaudeAgentSdkAdapter(AgentPort):
    """`AgentPort` implementation using `claude_agent_sdk.query`.

    Stateless — safe to share one instance across the process. The
    SDK manages its own auth state (env var) per call.
    """

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

        # Auth precheck. The SDK will raise its own auth error if this
        # is missing, but surface a sharper error so the settings page
        # can prompt the user to paste a `claude setup-token`.
        if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            raise AgentUnavailable(
                "CLAUDE_CODE_OAUTH_TOKEN not set — run `claude setup-token` and paste"
            )

        chosen_model = model or DEFAULT_MODEL
        chosen_reasoning = reasoning or DEFAULT_REASONING
        # SDK calls it `effort`, not `reasoning` (low | medium | high).
        # Construct via kwargs so older SDK versions without the field
        # surface a clean error instead of silently dropping it.
        options = ClaudeAgentOptions(model=chosen_model, effort=chosen_reasoning)

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
            raise AgentUnavailable(f"claude-agent-sdk error: {e}") from e

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
