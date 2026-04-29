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

from prep.agent.status import (
    init_availability,
    probe,
    set_available,
    status,
)


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
    "init_availability",
    "is_available",
    "probe",
    "set_available",
    "status",
]
