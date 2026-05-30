"""prep.agent.port — provider-agnostic interface for AI agent invocations.

This is the **domain port** for the agent bounded context: a thin
protocol + value objects, pure (no I/O), depended on by every caller
that wants to invoke an agent. Concrete implementations
(`sdk_adapter.ClaudeAgentSdkAdapter`, `fake.FakeAgent`) plug in via
adapter pattern.

The point of the indirection: if/when we swap Claude for another
provider, callers don't change — only the adapter does.

Cost data is part of the result so callers (or middleware) can persist
it to `agent_usage` for the per-token budget UI. Cost is the
adapter's best-effort estimate; treat it as approximate, never as
billing truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

# Default model + reasoning settings for the agent layer. Set here
# (one place) so per-callsite overrides are explicit. Sonnet at
# medium reasoning is the user-chosen baseline; Slice F may benchmark
# alternatives.
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_REASONING = "medium"


@dataclass(frozen=True)
class AgentResult:
    """Outcome of a single agent invocation.

    `text` is the freeform response (the part callers typically parse
    into structured data — JSON cards, plan items, etc.).

    The token / cost fields are provider-reported best-effort, may be
    None when the provider doesn't expose them. `cost_usd` is the
    canonical field the usage repo writes; the token counts are
    retained for after-the-fact analysis.
    """

    text: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None


class AgentUnavailable(RuntimeError):
    """Raised when the agent can't be reached or returns an unusable
    response. Callers catch this and degrade gracefully (e.g., trivia
    generation falls back to a friendly message). Matches the
    existing `prep.trivia.agent_client.AgentUnavailable` contract so
    callers can be ported one-by-one without changing their
    exception handling."""


class AgentBudgetExhausted(AgentUnavailable):
    """Subclass surfaced when the SDK reports that the user has hit
    their monthly Anthropic agent-SDK credit allocation (the per-
    plan $X/mo pool — $200 on Max 20x, $20 on Pro). Catch this
    specifically to render a "your Claude plan's monthly allocation
    is exhausted — resumes [next reset]" message instead of the
    generic "AI unavailable." Bare AgentUnavailable still works
    as a catch-all for legacy callers."""


class AgentPort(Protocol):
    """Provider-agnostic agent interface.

    Implementations must be safe to call from async code. Sync callers
    that need this should `anyio.from_thread.run` or similar — we
    don't ship a sync variant on the protocol because the SDK is
    async-native and a sync facade would just wrap `asyncio.run`.

    `model` is provider-namespaced (e.g. "claude-sonnet-4-6"). Pass
    None to accept the adapter's default.
    """

    async def run(
        self,
        prompt: str,
        *,
        model: str | None = None,
        reasoning: str | None = None,
        timeout_s: float = 120.0,
    ) -> AgentResult:
        """Execute `prompt` and return the result.

        Raises `AgentUnavailable` on any provider-side failure
        (auth, transport, rate-limit, malformed response).
        """
        ...
