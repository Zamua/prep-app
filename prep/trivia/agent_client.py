"""Python-side agent client — thin shim over `prep.agent` for legacy callers.

This module used to POST to the agent-server container's /run
endpoint over HTTP. As of the SDK migration it routes through
`prep.agent.get_agent()` (an in-process `claude-agent-sdk` adapter
by default; FakeAgent in tests). The public API is preserved so
existing callers (`trivia.service`, `trivia.routes`, `notify.scheduler`,
trivia.scheduler) don't need to change their imports.

What you get:
- `AgentUnavailable` — same name, same semantics; re-exported from
  `prep.agent.port`. Catching the old import path still works.
- `run_prompt(prompt, *, timeout_s)` — sync entry point. Wraps the
  async adapter via `asyncio.run`. Safe to call ONLY from non-async
  contexts (e.g., inside `asyncio.to_thread` from the scheduler);
  same constraint as before. Async callers must use `run_prompt_async`.
- `run_prompt_async(prompt, *, timeout_s)` — async entry point.

Usage is recorded (same `agent_usage` rollup the FastAPI handler
populates) so the per-token monthly total covers both worker calls
and Python-side calls.
"""

from __future__ import annotations

import asyncio
import logging
import os

from prep import agent as _agent_mod
from prep.agent.port import AgentUnavailable
from prep.agent.usage import AgentUsageRepo, hash_token

logger = logging.getLogger(__name__)

__all__ = ["AgentUnavailable", "run_prompt", "run_prompt_async"]


# Long timeout retained from the HTTP era — generation prompts can
# run 8-10 minutes on full deck batches; the SDK call inherits this
# as a wall-clock budget so a stalled provider surfaces a clean
# AgentUnavailable instead of a generic timeout up-stack.
_DEFAULT_TIMEOUT_S = 900.0


def _record_usage(result, *, user_login: str | None = None) -> None:
    """Append one agent_usage row. Best-effort; logs + swallows any
    repo failure so a transient SQLite hiccup never breaks the
    caller's workflow."""
    token = (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()
    if not token:
        return
    try:
        AgentUsageRepo().record(
            token_hash=hash_token(token),
            model=result.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=result.cost_usd,
            user_login=user_login,
        )
    except Exception:  # noqa: BLE001
        logger.exception("agent_usage record failed (non-fatal)")


def run_prompt(prompt: str, *, timeout_s: float = _DEFAULT_TIMEOUT_S) -> str:
    """Sync facade over the async adapter. Wraps `asyncio.run`, which
    creates a fresh event loop — must not be called from an existing
    one. The notify.scheduler tick is the only legit caller today;
    it runs the trivia refill under `asyncio.to_thread`, so this
    wrapper executes on a worker thread with no active loop.

    Caught the hard way at 19:30 UTC on 2026-05-07 (pre-migration):
    a request-path call blocked the event loop until the upstream
    timeout fired, taking down all request handling. The async
    variant (run_prompt_async) is the request-path safe option."""
    return asyncio.run(run_prompt_async(prompt, timeout_s=timeout_s))


async def run_prompt_async(prompt: str, *, timeout_s: float = _DEFAULT_TIMEOUT_S) -> str:
    """Async facade. Calls the configured `AgentPort` and returns the
    response text. Raises `AgentUnavailable` on adapter failure
    (matches the legacy contract — service-layer error handling
    doesn't change)."""
    result = await _agent_mod.get_agent().run(prompt, timeout_s=timeout_s)
    _record_usage(result)
    return result.text
