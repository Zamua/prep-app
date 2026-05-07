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

import httpx


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


def _agent_url() -> str:
    base = (os.environ.get("PREP_AGENT_URL") or "").strip()
    if not base:
        raise AgentUnavailable(
            "PREP_AGENT_URL is not set — trivia generation needs the "
            "agent container to be running and reachable."
        )
    return base.rstrip("/") + "/run"


def run_prompt(prompt: str, *, timeout_s: float = _DEFAULT_TIMEOUT_S) -> str:
    """POST a single prompt to the agent's /run endpoint and return
    its stdout. Raises `AgentUnavailable` on transport / HTTP errors.

    Synchronous version, used by trivia generation (which runs on the
    Temporal worker, not on the request path). Request-path callers
    (grading) must use `run_prompt_async` instead — a slow agent call
    in a sync def blocks Starlette's threadpool, which is what took
    prod down on 2026-05-07.
    """
    url = _agent_url()
    body = json.dumps({"prompt": prompt}).encode("utf-8")
    req = urllib.request.Request(
        url,
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


async def run_prompt_async(prompt: str, *, timeout_s: float = _DEFAULT_TIMEOUT_S) -> str:
    """Async counterpart to `run_prompt`, for callers that run on the
    request path. Uses httpx.AsyncClient so a slow agent call yields
    the event loop instead of holding a Starlette threadpool slot.
    Same `AgentUnavailable` contract as the sync version."""
    url = _agent_url()
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.post(url, json={"prompt": prompt})
            if r.status_code != 200:
                try:
                    err = r.json().get("error", "")
                except Exception:
                    err = r.text
                raise AgentUnavailable(f"agent HTTP {r.status_code}: {err}")
            data = r.json()
            if "stdout" not in data:
                raise AgentUnavailable(f"agent returned no stdout: {str(data)[:300]}")
            return data["stdout"]
    except (httpx.TimeoutException, httpx.TransportError) as e:
        raise AgentUnavailable(f"agent unreachable: {e}") from e
