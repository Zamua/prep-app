"""Agent CLI invocation helper.

prep shells out to a local AI CLI (claude-code by default) for question
generation, grading, and transform work — rather than using an API key.
This keeps the user's subscription credentials in the CLI's keychain and
lets them swap to other harnesses (opencode, aider, …) by env var.

Two env vars configure it (defaults match claude-code on macOS):

    PREP_AGENT_BIN  = ~/.local/bin/claude
    PREP_AGENT_ARGS = --strict-mcp-config,--mcp-config,{mcp_config},-p

Args are comma-separated. Placeholders supported:

    {mcp_config}   — replaced by an empty-MCP JSON literal so the CLI
                     doesn't load the user's plugin config and try to
                     spawn its own MCP children. Claude-specific; other
                     agents can omit it.

The prompt is appended after the rendered args. CLAUDE_BIN is honored
as a backward-compat alias for PREP_AGENT_BIN.
"""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_BIN = str(Path.home() / ".local" / "bin" / "claude")
_DEFAULT_ARGS = "--strict-mcp-config,--mcp-config,{mcp_config},-p"
_EMPTY_MCP_CONFIG = '{"mcpServers":{}}'


def agent_command(prompt: str) -> list[str]:
    """Return argv for a one-shot agent invocation."""
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
