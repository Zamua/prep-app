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


def status() -> dict:
    """Return a structured agent status dict the UI can render.

    Shape:
      {
        "kind":         "http" | "shell" | "unconfigured",
        "logged_in":    bool,
        "email":        str (optional),
        "org_name":     str (optional),
        "subscription_type": str (optional),
        "reason":       str (optional, when logged_in is False),
      }

    "logged_in" is the canonical "AI features should light up" flag —
    matches the previous `probe()` boolean.
    """
    url = (os.environ.get("PREP_AGENT_URL") or "").strip()
    if url:
        out = {"kind": "http", "logged_in": False}
        try:
            with urllib.request.urlopen(
                url.rstrip("/") + "/healthz", timeout=2.0
            ) as resp:
                if 200 <= resp.status < 300:
                    body = resp.read().decode("utf-8", errors="replace")
                    import json as _json
                    try:
                        data = _json.loads(body)
                        out["logged_in"] = bool(data.get("logged_in"))
                        for k in ("email", "org_name", "subscription_type", "reason"):
                            if data.get(k):
                                out[k] = data[k]
                    except _json.JSONDecodeError:
                        out["reason"] = "agent-server returned non-JSON"
                else:
                    out["reason"] = f"agent-server returned HTTP {resp.status}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            out["reason"] = f"agent-server unreachable: {e}"
        return out

    bin_path = (
        (os.environ.get("PREP_AGENT_BIN") or "").strip()
        or (os.environ.get("CLAUDE_BIN") or "").strip()
    )
    if not bin_path:
        bin_path = _DEFAULT_BIN
        if not (os.path.isfile(bin_path) and os.access(bin_path, os.X_OK)):
            return {"kind": "unconfigured", "logged_in": False,
                    "reason": "neither PREP_AGENT_URL nor PREP_AGENT_BIN is set"}
    available = os.path.isfile(bin_path) and os.access(bin_path, os.X_OK)
    out = {"kind": "shell", "logged_in": available}
    if not available:
        out["reason"] = f"PREP_AGENT_BIN={bin_path!r} doesn't exist or isn't executable"
    return out


def probe() -> bool:
    """Boolean view of `status()` — whether AI features should light up.
    Kept as a helper for code paths that just want True/False."""
    return bool(status().get("logged_in"))
