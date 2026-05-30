"""Agent availability probe — used by the FastAPI app at startup.

Post-SDK-migration: the probe is purely local. We check whether a
Claude OAuth token is present in the env (or in the prep-data
token file) AND the `claude_agent_sdk` package can be imported.
That's "the SDK can run if asked" — same semantic the old
agent-server-/healthz check provided.

Result feeds the `agent_available` Jinja context flag, which gates
AI surfaces in the UI.
"""

from __future__ import annotations

import logging
import os

from prep.agent import token_store

logger = logging.getLogger(__name__)


def status() -> dict:
    """Return a structured agent status dict the UI can render.

    Shape:
      {
        "kind":         "sdk" | "unconfigured",
        "logged_in":    bool,
        "reason":       str (optional, when logged_in is False),
      }

    Token sources (in precedence):
      1. CLAUDE_CODE_OAUTH_TOKEN env var (set explicitly or loaded
         from the token file at boot)
      2. The token file at PREP_DATA_DIR/claude-oauth-token

    We don't probe `import claude_agent_sdk` here — the package is a
    hard dependency declared in pyproject; if it's missing prep won't
    boot, and the probe import was adding ~2s per call which made
    test runs 70× slower (status() fires per test-client construction).
    """
    have_env = bool((os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip())
    have_file = token_store.token_exists()
    if not (have_env or have_file):
        return {
            "kind": "unconfigured",
            "logged_in": False,
            "reason": "no CLAUDE_CODE_OAUTH_TOKEN — paste a `claude setup-token` value",
        }
    return {"kind": "sdk", "logged_in": True}


def probe() -> bool:
    """Boolean view of `status()` — whether AI features should light up."""
    return bool(status().get("logged_in"))


# Module-level cache of the boot probe. Templates read this via the
# Jinja context_processor in prep.web.templates so AI-driven UI is
# gated everywhere consistently. /settings/agent/connect updates it
# after a fresh token is pasted; /settings/agent/disconnect clears
# it. set_available() is the only blessed mutation path so the UI
# (templates), routes, and the probe stay in sync.
is_available: bool = False


def set_available(value: bool) -> None:
    """Update the cached agent availability flag. Routes call this
    after /connect or /disconnect lands; templates read the new value
    on the next render."""
    global is_available
    is_available = bool(value)


def init_availability() -> None:
    """Re-run the probe and cache the result. Called once at app
    startup; also loads the token file into the process env if
    present (so the SDK can pick it up without a container restart).

    No retries needed any more — the probe is local-only (file +
    import check), unlike the prior HTTP-to-agent-server probe."""
    token_store.load_into_env()
    set_available(probe())
