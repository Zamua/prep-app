"""prep.agent.fake — in-memory `AgentPort` implementation for tests.

Records every call and returns canned responses. Use it via
dependency injection in route + service tests so we never touch a
real provider in the suite.

Pattern matches the existing `prep.trivia.agent_client` test
monkeypatch (just `setattr(svc, 'run_prompt', ...)`) — this is the
typed, reusable version.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from prep.agent.port import AgentResult, AgentUnavailable


@dataclass
class FakeAgent:
    """Test double for `AgentPort`.

    Configure `next_response` (or a queue via `responses`) and the
    fake will return them in order. Set `raise_unavailable=True` to
    simulate provider outage.

    Inspect `calls` (list of dicts) after exercising the SUT to
    assert on what prompt / model / reasoning was actually
    requested.
    """

    next_response: AgentResult | None = None
    responses: list[AgentResult] = field(default_factory=list)
    raise_unavailable: bool = False
    calls: list[dict] = field(default_factory=list)

    async def run(
        self,
        prompt: str,
        *,
        model: str | None = None,
        reasoning: str | None = None,
        timeout_s: float = 120.0,
    ) -> AgentResult:
        self.calls.append(
            {
                "prompt": prompt,
                "model": model,
                "reasoning": reasoning,
                "timeout_s": timeout_s,
            }
        )
        if self.raise_unavailable:
            raise AgentUnavailable("fake agent configured to fail")
        if self.responses:
            return self.responses.pop(0)
        if self.next_response is not None:
            return self.next_response
        # Sensible default so tests that don't care about response
        # content don't have to construct one. Cost is zero so it
        # doesn't pollute usage rollups.
        return AgentResult(
            text="(fake agent response)",
            model=model or "fake-model",
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
        )
