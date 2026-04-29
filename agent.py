"""Agent availability probe — used by the FastAPI app at startup.

The actual agent shell-out happens entirely in the Go worker; this
module exists so the Python side can answer "is AI configured + reachable
right now?" without taking a hard dependency on the worker boot order.

The probe checks:
  1. PREP_AGENT_URL → GET <url>/healthz, parse JSON, surface logged_in.
  2. PREP_AGENT_BIN (or CLAUDE_BIN) → check the file is executable.
  3. ~/.local/bin/claude — last-resort default that maps to the
     conventional Claude Code installer path.

Result feeds the `agent_available` jinja context flag, which gates AI
surfaces in the UI.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

_DEFAULT_BIN = str(Path.home() / ".local" / "bin" / "claude")


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
                    try:
                        data = json.loads(body)
                        out["logged_in"] = bool(data.get("logged_in"))
                        for k in ("email", "org_name", "subscription_type", "reason"):
                            if data.get(k):
                                out[k] = data[k]
                    except json.JSONDecodeError:
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
    """Boolean view of `status()` — whether AI features should light up."""
    return bool(status().get("logged_in"))
