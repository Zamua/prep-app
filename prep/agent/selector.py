"""Per-user `AgentPort` selector — the seam between callers and
adapters.

Routes / services / the worker callback hit `agent_for_user(uid)` and
get back an adapter ready to run. The selector consults BYOK first
(the user's own provider key), then the deploy-wide
subscription-token adapter, then the deploy-configured free tier. A
noop adapter is returned when none of those is available - the noop
is the ONE place we centralize the "AI is not configured" failure.

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

import json
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
# providers and hasn't set an explicit active choice, the first in
# this list wins. CLAUDE_SUBSCRIPTION first: it draws from the
# user's flat-rate Max-plan credit pool (no per-token surprise
# charges), so when a user configures both subscription + API key
# we'd rather burn the subscription quota by default. ANTHROPIC_API
# second (same model surface, just metered). Then OpenRouter as the
# multi-vendor router, then OpenAI as a fallback.
_BYOK_PROVIDER_ORDER = (
    Provider.CLAUDE_SUBSCRIPTION,
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
    if provider is Provider.CLAUDE_SUBSCRIPTION:
        # Same SDK adapter as the deploy-wide path, but with the
        # user's token bound at construction so it's injected via
        # ClaudeAgentOptions.env (per-subprocess) instead of read
        # from process env. Concurrency-safe across users.
        from prep.agent.sdk_adapter import ClaudeAgentSdkAdapter

        return ClaudeAgentSdkAdapter(token=secret)
    raise ValueError(f"unsupported BYOK provider: {provider}")


def _subscription_path_allowed() -> bool:
    """Is the deploy-wide subscription OAuth token a legal fallback?

    YES on single-user local installs (Tailscale / fake providers) —
    one person operates the deploy AND consumes the AI; the token
    funds their own use.

    NO on multi-user public deploys (Clerk mode): every signup would
    silently consume the operator's subscription credit pool without
    knowing it. The subscription path is hard-gated off for
    `PREP_AUTH_MODE=clerk`, even if `CLAUDE_CODE_OAUTH_TOKEN` is
    somehow set.

    See also: prep/agent/routes.py — the /settings/agent/connect POST
    refuses to save a token under the same condition; settings_agent.html
    doesn't render the connect form. Defense in depth: even if those
    surfaces were bypassed, this guard makes the path inert.
    """
    return (os.environ.get("PREP_AUTH_MODE") or "tailscale").strip().lower() != "clerk"


# ---- free tier -----------------------------------------------------------

_FREE_ENV_BASE_URL = "PREP_FREE_INFERENCE_BASE_URL"
_FREE_ENV_API_KEY = "PREP_FREE_INFERENCE_API_KEY"
_FREE_ENV_MODEL = "PREP_FREE_INFERENCE_MODEL"
_FREE_ENV_EXTRA_BODY = "PREP_FREE_INFERENCE_EXTRA_BODY"

# Output cap for free-tier calls. The deck-wide transform's output
# scales with deck size (old + new content per card), so the compat
# adapter's 4096 default would truncate large decks mid-object; the
# endpoint models carry 262K+ context, so the cap is ours to choose.
_FREE_TIER_MAX_TOKENS = 32768


def free_tier_agent(max_output_tokens: int | None = None) -> AgentPort | None:
    """Build the deploy-configured free-tier adapter, or None.

    Configured iff BASE_URL + API_KEY + MODEL are all set. NEVER
    RAISES: this runs (via `agent_for_user`) inside the template
    context processor on every page render, so an escaping exception
    would be a deploy-wide 500, not a degraded AI feature. Every
    config failure logs at ERROR (operator-actionable) and returns
    None (free tier off).

    `max_output_tokens` overrides the transform-sized default output
    cap; None keeps `_FREE_TIER_MAX_TOKENS`."""
    try:
        base_url = (os.environ.get(_FREE_ENV_BASE_URL) or "").strip()
        api_key = (os.environ.get(_FREE_ENV_API_KEY) or "").strip()
        model = (os.environ.get(_FREE_ENV_MODEL) or "").strip()
        present = {
            _FREE_ENV_BASE_URL: base_url,
            _FREE_ENV_API_KEY: api_key,
            _FREE_ENV_MODEL: model,
        }
        if not any(present.values()):
            return None  # feature off - the normal, silent case
        missing = [name for name, value in present.items() if not value]
        if missing:
            logger.error(
                "free tier disabled: half-configured - %s set but %s missing",
                ", ".join(n for n, v in present.items() if v),
                ", ".join(missing),
            )
            return None

        # Parse extra_body ONCE here, never per request. A parse
        # failure disables the free tier rather than shipping a
        # request body we don't understand.
        extra_body: dict | None = None
        raw_extra = (os.environ.get(_FREE_ENV_EXTRA_BODY) or "").strip()
        if raw_extra:
            try:
                parsed = json.loads(raw_extra)
            except ValueError:
                logger.error("free tier disabled: %s is not valid JSON", _FREE_ENV_EXTRA_BODY)
                return None
            if not isinstance(parsed, dict):
                logger.error(
                    "free tier disabled: %s must be a JSON object, got %s",
                    _FREE_ENV_EXTRA_BODY,
                    type(parsed).__name__,
                )
                return None
            extra_body = parsed

        from prep.agent.openai_compat import OpenAICompatAdapter

        try:
            return OpenAICompatAdapter(
                api_key,
                base_url=base_url,
                model=model,
                extra_body=extra_body,
                shared=True,
                max_tokens=(
                    max_output_tokens if max_output_tokens is not None else _FREE_TIER_MAX_TOKENS
                ),
                provider_label="free AI",
            )
        except Exception:
            logger.exception("free tier disabled: adapter construction failed")
            return None
    except Exception:
        # Belt over the branches above: the never-raises contract
        # holds even for failure shapes not anticipated here.
        logger.exception("free tier disabled: unexpected factory failure")
        return None


def free_tier_configured() -> bool:
    """Whether the deploy's free tier would actually serve - i.e. the
    factory yields an adapter, not merely that some env vars exist.
    Drives the settings-page state split."""
    return free_tier_agent() is not None


def agent_available_for_user(user_id: str | None) -> bool:
    """Per-user availability — drives the `agent_available` template
    flag. True when `agent_for_user(uid)` would return a usable
    adapter (BYOK row decrypts, deploy-wide subscription path is
    legal + a token is set, or the free tier is configured). False
    when the only thing the selector can hand back is `_NoopAgent`.

    Why per-user, not global: on a multi-user clerk deploy, "is the
    agent available" is meaningless deploy-wide — every user owns
    their own credentials, so a deploy-wide probe misses every BYOK
    row. This helper resolves availability for the request user;
    routes/context-processors call it with that user.
    """
    return not isinstance(agent_for_user(user_id), _NoopAgent)


def _user_has_byok_rows(user_id: str) -> bool:
    """Whether the user has ANY stored BYOK credential row.

    Deliberately decrypt-free (a bare SELECT): callable on deploys
    with no master key configured, where BYOKRepo() itself raises.
    The selector uses it to decide between fall-through (no rows,
    nothing to protect) and fail-closed (rows present but unusable)."""
    from prep.infrastructure.db import cursor

    with cursor() as c:
        row = c.execute(
            "SELECT 1 FROM byok_credentials WHERE user_id = ? LIMIT 1",
            (user_id,),
        ).fetchone()
    return row is not None


def agent_for_user(user_id: str | None) -> AgentPort:
    """Return the AgentPort that should be used for this user's call.

    Selection order (first hit wins):
      1. Per-user BYOK key (precedence per `_BYOK_PROVIDER_ORDER`)
      2. Deploy-wide subscription OAuth token (single-user local
         installs only — `_subscription_path_allowed()` returns False
         on `PREP_AUTH_MODE=clerk` to keep the operator's credit pool
         from funding random signups on a public deploy)
      3. Deploy-configured free tier (`free_tier_agent()`)
      4. `_NoopAgent` whose `.run()` raises AgentUnavailable

    Only "no BYOK row for this user" continues past step 1. Any
    exception inside the BYOK step returns `_NoopAgent` immediately:
    BYOK is the privacy opt-out from the shared free tier, so a user
    whose configured key path fails must never be silently served by
    the shared endpoint - and silently switching credentials would
    mask the broken key.

    `user_id` may be None for system-initiated calls. When None,
    BYOK is skipped and selection starts at the subscription path.
    """
    if _factory_override is not None:
        return _factory_override(user_id)

    # 1. BYOK first — user's own key wins. Honor the user's explicit
    # provider choice (active_byok_provider on the users row) when
    # set, before falling back to the built-in precedence order.
    if user_id:
        # Row existence is checked WITHOUT touching the master key:
        # BYOKRepo() eagerly loads it, so constructing the repo on a
        # deploy that never configured PREP_KEY_ENCRYPTION_SECRET
        # raises MasterKeyError before any lookup. A user with NO
        # stored key has nothing that could be downgraded, so that
        # deploy shape must fall through to the shared providers, not
        # fail closed (it used to kill ALL AI on master-key-less
        # installs). Only a user who HAS a row gets the fail-closed
        # treatment below.
        try:
            has_rows = _user_has_byok_rows(user_id)
        except Exception:  # noqa: BLE001
            logger.exception("agent: BYOK row check failed for user %s", user_id)
            has_rows = True  # unknown state: treat as having keys, fail closed below
    else:
        has_rows = False
    if user_id and has_rows:
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
            # Fail loud, fail closed. A row-lookup / decrypt /
            # adapter-construction failure means this user HAS (or may
            # have) a configured key we can't use - never downgrade
            # them to a shared credential they opted out of.
            logger.exception(
                "agent: BYOK path failed for user %s; AI unavailable for this request",
                user_id,
            )
            return _NoopAgent(
                "AI is unavailable: your saved API key could not be used. "
                "Re-add it on /settings/agent, or ask the deploy admin to "
                "check the server logs."
            )

    # 2. Deploy-wide subscription OAuth token (single-user local
    #    installs only). `_subscription_path_allowed()` returns False
    #    on clerk-mode deploys so a stray CLAUDE_CODE_OAUTH_TOKEN
    #    can't silently fund every signup from the operator's credit
    #    pool.
    if _subscription_path_allowed() and (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip():
        from prep.agent.sdk_adapter import ClaudeAgentSdkAdapter

        return ClaudeAgentSdkAdapter()

    # 3. Deploy-configured free tier.
    free = free_tier_agent()
    if free is not None:
        return free

    # 4. Nothing configured.
    return _NoopAgent(
        "no AI agent configured — add a personal API key on /settings/agent, "
        "or the deploy admin can paste a subscription OAuth token."
    )
