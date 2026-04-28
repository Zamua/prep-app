"""Agent CLI / HTTP invocation helper + availability probe.

prep shells out to a local AI CLI (claude-code by default) for question
generation, grading, and transform work — rather than using an API key.
This keeps the user's subscription credentials in the CLI's keychain and
lets them swap to other harnesses (opencode, aider, …) by env var.

Configuration:
    PREP_AGENT_URL  = http://host:9999     (preferred when running
                                            containerized; talks to a
                                            host-side agent-server)
    PREP_AGENT_BIN  = ~/.local/bin/claude  (fallback: direct CLI shell-out)
    PREP_AGENT_ARGS = --strict-mcp-config,--mcp-config,{mcp_config},-p

PREP_AGENT_URL takes precedence over PREP_AGENT_BIN if both are set.
CLAUDE_BIN is honored as a backward-compat alias for PREP_AGENT_BIN.

The agent CLI invocation logic itself lives in the Go worker (see
worker-go/agent/). This module only provides:
  * `agent_command(prompt)` — argv for legacy synchronous Python paths
    (grader.py, generator.py — kept for ad-hoc CLI use).
  * `probe()` — returns whether AI is reachable at boot. Used by
    app.py's startup to set `app.state.agent_available` so templates
    can hide AI-driven UI when no agent is configured.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from pathlib import Path

_DEFAULT_BIN = str(Path.home() / ".local" / "bin" / "claude")
_DEFAULT_ARGS = "--strict-mcp-config,--mcp-config,{mcp_config},-p"
_EMPTY_MCP_CONFIG = '{"mcpServers":{}}'


def agent_command(prompt: str) -> list[str]:
    """Return argv for a one-shot agent invocation (legacy CLI path)."""
    bin_path = (
        os.environ.get("PREP_AGENT_BIN")
        or os.environ.get("CLAUDE_BIN")
        or _DEFAULT_BIN
    )
    args_csv = os.environ.get("PREP_AGENT_ARGS") or _DEFAULT_ARGS
    args: list[str] = []
    for a in args_csv.split(","):
        a = a.strip()
        if not a:
            continue
        a = a.replace("{mcp_config}", _EMPTY_MCP_CONFIG)
        args.append(a)
    return [bin_path, *args, prompt]


def probe() -> bool:
    """Return whether an AI agent is reachable.

    Resolution order matches the Go worker's agent.FromEnv():
      1. PREP_AGENT_URL set → GET <url>/healthz, accept any 2xx.
      2. PREP_AGENT_BIN (or CLAUDE_BIN) set → check the file exists.
      3. Default ~/.local/bin/claude → check it exists.
      4. Otherwise False.

    Network errors / timeouts on the URL probe count as unavailable —
    we'd rather show "AI off" than block app startup on a dead server.
    """
    url = (os.environ.get("PREP_AGENT_URL") or "").strip()
    if url:
        try:
            with urllib.request.urlopen(
                url.rstrip("/") + "/healthz", timeout=2.0
            ) as resp:
                return 200 <= resp.status < 300
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    bin_path = (
        (os.environ.get("PREP_AGENT_BIN") or "").strip()
        or (os.environ.get("CLAUDE_BIN") or "").strip()
        or _DEFAULT_BIN
    )
    return os.path.isfile(bin_path) and os.access(bin_path, os.X_OK)
