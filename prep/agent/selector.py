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


# Provider precedence — when a user has BYOK keys for multiple
# providers, the first in this list wins. Order chosen so the
# narrowest / most-specific provider beats the generic chat-completions
# providers (OpenRouter routes through Anthropic anyway, so direct
# Anthropic when available; then OpenRouter as the multi-vendor router;
# then OpenAI as the fallback).
_BYOK_PROVIDER_ORDER = (
    Provider.ANTHROPIC_API,
    Provider.OPENROUTER_API,
    Provider.OPENAI_API,
)


def _build_byok_adapter(provider: Provider, secret: str) -> AgentPort:
    """Construct the concrete adapter for a (provider, secret) pair.
    Late-imports the adapter modules so a Tailscale-mode deploy doesn't
    pay the httpx-AsyncClient construction cost just for prep.agent
    to load."""
    if provider is Provider.ANTHROPIC_API:
        from prep.agent.anthropic_api import AnthropicApiAdapter

        return AnthropicApiAdapter(secret)
    if provider is Provider.OPENROUTER_API:
        from prep.agent.openrouter import OpenRouterAdapter

        return OpenRouterAdapter(secret)
    if provider is Provider.OPENAI_API:
        from prep.agent.openai_api import OpenAIAdapter

        return OpenAIAdapter(secret)
    raise ValueError(f"unsupported BYOK provider: {provider}")


def agent_for_user(user_id: str | None) -> AgentPort:
    """Return the AgentPort that should be used for this user's call.

    Selection order (first hit wins):
      1. Per-user BYOK key (Anthropic → OpenRouter → OpenAI in order)
      2. Deploy-wide subscription OAuth token
         (`CLAUDE_CODE_OAUTH_TOKEN` env or token file)
      3. `_NoopAgent` whose `.run()` raises AgentUnavailable

    `user_id` may be None for system-initiated calls (none today;
    every AI invocation is per-user). When None, BYOK is skipped
    and we go straight to the subscription path.
    """
    if _factory_override is not None:
        return _factory_override(user_id)

    # 1. BYOK first — user's own key wins. Honor the user's explicit
    # provider choice (active_byok_provider on the users row) when
    # set, before falling back to the built-in precedence order.
    if user_id:
        try:
            from prep.auth.repo import UserRepo
            from prep.byok.repo import BYOKRepo

            byok_repo = BYOKRepo()

            chosen = UserRepo().get_active_byok_provider(user_id)
            order: list[Provider] = []
            if chosen:
                try:
                    chosen_p = Provider(chosen)
                    if chosen_p in _BYOK_PROVIDER_ORDER:
                        order.append(chosen_p)
                except ValueError:
                    # Stale / unknown enum value on the row — skip and
                    # fall through to defaults. We don't clear it here;
                    # the /settings/agent render path handles cleanup.
                    pass
            for p in _BYOK_PROVIDER_ORDER:
                if p not in order:
                    order.append(p)

            for provider in order:
                secret = byok_repo.get_secret(user_id=user_id, provider=provider)
                if secret:
                    logger.debug("agent: using BYOK %s for user %s", provider.value, user_id)
                    return _build_byok_adapter(provider, secret)
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
        "no AI agent configured — add a personal API key on /settings/agent, "
        "or the deploy admin can paste a subscription OAuth token."
    )
