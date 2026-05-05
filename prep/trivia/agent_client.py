"""Python-side HTTP client for the agent container.

Trivia generation is a single one-shot claude call (in vs the
multi-step plan/expand flow, which earns its keep on Temporal). We
keep it out of the worker entirely and POST directly from the FastAPI
process — same wire format the Go worker uses (`POST /run` →
`{"stdout"}`), just from Python so the trivia service stays
self-contained in `prep.trivia`.

If the agent is unreachable, callers see `AgentUnavailable` and
should treat trivia generation as best-effort skipped (the scheduler
will retry on the next tick).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


class AgentUnavailable(RuntimeError):
    """Raised when the agent container can't be reached or returns
    an error. Caller decides whether to log + skip or surface."""


# Generation prompts ask for ~25 questions including a regex per
# card. Claude often takes 15-25s per card these days; a full batch
# can run 8-10 minutes, so we give a generous wall-clock budget.
# Falling well short of this would have us silently dropping work
# claude already did (the agent finishes server-side after the prep
# client closes).
_DEFAULT_TIMEOUT_S = 900.0


def run_prompt(prompt: str, *, timeout_s: float = _DEFAULT_TIMEOUT_S) -> str:
    """POST a single prompt to the agent's /run endpoint and return
    its stdout. Raises `AgentUnavailable` on transport / HTTP errors.
    """
    base = (os.environ.get("PREP_AGENT_URL") or "").strip()
    if not base:
        raise AgentUnavailable(
            "PREP_AGENT_URL is not set — trivia generation needs the "
            "agent container to be running and reachable."
        )

    body = json.dumps({"prompt": prompt}).encode("utf-8")
    req = urllib.request.Request(
        base.rstrip("/") + "/run",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            data = json.load(r)
            if "stdout" not in data:
                raise AgentUnavailable(f"agent returned no stdout: {str(data)[:300]}")
            return data["stdout"]
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode(errors="replace")).get("error", "")
        except Exception:
            err = str(e)
        raise AgentUnavailable(f"agent HTTP {e.code}: {err}") from e
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise AgentUnavailable(f"agent unreachable: {e}") from e
