"""prep.agent — AI agent integration.

Public surface (preserved across the package conversion):
- `status()`             — structured status of the configured agent
- `probe()`              — boolean whether AI features should light up
- `is_available`         — module-level cached probe result
- `set_available(bool)`  — update the cache (used by /settings/agent)
- `init_availability()`  — re-run probe + update cache (called at boot)

Implementation lives in prep/agent/status.py; this file re-exports
the public surface so existing call sites (`from prep import agent
as _agent_mod`, then `_agent_mod.is_available`) keep working.

The `is_available` re-export is a thin __getattr__ shim — the
underlying value is module-level on prep.agent.status, so we
proxy attribute access there to avoid a stale cached copy on the
package itself.
"""

from prep.agent.port import AgentPort
from prep.agent.status import (
    init_availability,
    probe,
    set_available,
    status,
)

# Test-injected override. Production code goes through
# `agent_for_user(user_id)` → prep.agent.selector, which consults
# BYOK + subscription-token in precedence and returns the right
# adapter. `set_agent()` skips that selection entirely; tests use it
# to inject a FakeAgent without touching DB or env.
_agent_override: AgentPort | None = None


def get_agent(user_id: str | None = None) -> AgentPort:
    """Return the `AgentPort` for this user's AI call.

    Pass the calling user's ID so per-user BYOK keys are honored.
    With `user_id=None` the selector skips BYOK and falls back to
    the deploy-wide subscription OAuth token. See
    `prep.agent.selector.agent_for_user` for the full precedence.

    Tests can `set_agent(FakeAgent())` to override regardless of
    user / DB / env — that path bypasses the selector entirely.
    """
    if _agent_override is not None:
        return _agent_override
    from prep.agent.selector import agent_for_user

    return agent_for_user(user_id)


def is_available_for(user_id: str | None) -> bool:
    """Per-user availability — the multi-user safe replacement for the
    module-level `is_available` flag. Returns True when this user's
    BYOK row (or the deploy-wide subscription path, when allowed)
    would yield a usable adapter. Use this in routes/templates that
    have a resolved user; the legacy `is_available` only knew about
    the deploy-wide file/env and misses every BYOK row on prepcards.app.

    Test overrides via `set_agent()` are honored — when the global
    override is set, that adapter's existence implies availability."""
    if _agent_override is not None:
        # Test override is in place; the override IS the configured
        # agent. Treat as available unless the override deliberately
        # exposes a falsy `available` attribute (escape hatch for
        # tests that want to simulate the "no AI" surface).
        return getattr(_agent_override, "available", True)

    # Deploy-wide flag wins when True. Covers (a) single-user tailscale
    # installs where CLAUDE_CODE_OAUTH_TOKEN is set on the container and
    # the selector's subscription path is the right answer, and
    # (b) legacy tests that just toggle `prep.agent.is_available = True`
    # without setting up BYOK or env. Returning True here short-circuits
    # the BYOK lookup, which is correct: the deploy-wide token covers
    # everyone on a single-tenant install.
    # Read via globals() to honor a test-shadowed value before falling
    # back to the backing module's live attribute.
    shadowed = globals().get("is_available")
    if shadowed is not None:
        if shadowed:
            return True
    else:
        from prep.agent import status as _status

        if _status.is_available:
            return True

    # Deploy-wide off (e.g. clerk-mode multi-user, no shared token):
    # ask the selector whether THIS user has a BYOK row that would
    # resolve to a usable adapter.
    from prep.agent.selector import agent_available_for_user

    return agent_available_for_user(user_id)


def set_agent(impl: AgentPort | None) -> None:
    """Replace the agent globally — tests only.

    Pass None to restore the default selector-driven behavior. The
    BYOK-then-subscription precedence in `selector.agent_for_user`
    is the real entry point in production.
    """
    global _agent_override
    _agent_override = impl


def __getattr__(name: str):
    """Module-level __getattr__ proxies live attribute access to the
    backing implementation module, so `prep.agent.is_available` always
    reflects the latest cached value (rather than a snapshot at the
    time prep.agent was first imported)."""
    if name == "is_available":
        # importlib bypasses the `from prep.agent.status import status`
        # shadow on this package's namespace.
        import importlib

        return importlib.import_module("prep.agent.status").is_available
    raise AttributeError(name)


__all__ = [
    "AgentPort",
    "get_agent",
    "init_availability",
    "is_available",
    "is_available_for",
    "probe",
    "set_agent",
    "set_available",
    "status",
]
