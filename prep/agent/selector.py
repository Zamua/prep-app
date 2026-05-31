"""Per-user `AgentPort` selector — the seam between callers and
adapters.

Routes / services / the worker callback hit `agent_for_user(uid)` and
get back an adapter ready to run. The selector consults BYOK first
(per-user Anthropic API key) and falls back to the deploy-level SDK
adapter (subscription OAuth token) if there's no BYOK row. A noop
adapter is returned only when neither is available — the noop is the
ONE place we centralize the "AI is not configured" failure.

This indirection means individual call sites don't branch on auth
shape:

    agent = agent_for_user(uid)
    result = await agent.run(prompt)        # raises AgentUnavailable
                                             # if not configured

The exception handler at the route layer renders the appropriate
"configure your agent" page once, instead of every route checking
`if agent_available else show_error`.

## Test override

`set_user_agent_factory(fn)` lets tests inject a function that
returns whatever adapter they want regardless of DB state. Pair
with the standard fixture teardown to restore the default.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from prep.agent.port import AgentPort, AgentResult, AgentUnavailable
from prep.byok.entities import Provider

logger = logging.getLogger(__name__)


# ---- noop adapter --------------------------------------------------------


class _NoopAgent(AgentPort):
    """Stand-in returned when no real adapter is available. Every
    `.run()` raises AgentUnavailable so callers don't need a
    pre-check — the same exception path covers "no key configured"
    and "configured key broke at call time."""

    def __init__(self, reason: str):
        self._reason = reason

    async def run(
        self,
        prompt: str,  # noqa: ARG002
        *,
        model: str | None = None,  # noqa: ARG002
        reasoning: str | None = None,  # noqa: ARG002
        timeout_s: float = 120.0,  # noqa: ARG002
    ) -> AgentResult:
        raise AgentUnavailable(self._reason)


# ---- selector ------------------------------------------------------------

_factory_override: Callable[[str | None], AgentPort] | None = None


def set_user_agent_factory(fn: Callable[[str | None], AgentPort] | None) -> None:
    """Test seam — replace the agent selector for the rest of the
    process. Pass `None` to restore the default behavior. The
    standard test fixture pattern is::

        prev = selector._factory_override
        set_user_agent_factory(lambda uid: FakeAgent())
        try:
            ...
        finally:
            set_user_agent_factory(prev)
    """
    global _factory_override
    _factory_override = fn


def agent_for_user(user_id: str | None) -> AgentPort:
    """Return the AgentPort that should be used for this user's call.

    Selection order (first hit wins):
      1. Per-user BYOK Anthropic API key (decrypted from
         `byok_credentials`)
      2. Deploy-wide subscription OAuth token
         (`CLAUDE_CODE_OAUTH_TOKEN` env or token file)
      3. `_NoopAgent` whose `.run()` raises AgentUnavailable

    `user_id` may be None for system-initiated calls (none today;
    every AI invocation is per-user). When None, BYOK is skipped
    and we go straight to the subscription path.
    """
    if _factory_override is not None:
        return _factory_override(user_id)

    # 1. BYOK first — user's own key wins.
    if user_id:
        try:
            from prep.byok.repo import BYOKRepo

            secret = BYOKRepo().get_secret(user_id=user_id, provider=Provider.ANTHROPIC_API)
            if secret:
                from prep.agent.anthropic_api import AnthropicApiAdapter

                logger.debug("agent: using BYOK Anthropic API key for user %s", user_id)
                return AnthropicApiAdapter(secret)
        except Exception:  # noqa: BLE001
            # BYOK lookup / decryption is fail-loud on the affected
            # request (the noop will raise AgentUnavailable with a
            # clear message), but should never break the broader
            # selector — if BYOK is broken, falling through to the
            # subscription path keeps prep working for the non-BYOK
            # user.
            logger.exception("agent: BYOK lookup failed for user %s; falling through", user_id)

    # 2. Subscription OAuth token — same path as before BYOK landed.
    if (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip():
        from prep.agent.sdk_adapter import ClaudeAgentSdkAdapter

        return ClaudeAgentSdkAdapter()

    # 3. Nothing configured.
    return _NoopAgent(
        "no AI agent configured — set up a personal API key on /settings/agent, "
        "or the deploy admin can paste a subscription OAuth token."
    )
