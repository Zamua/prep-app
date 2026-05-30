"""HTTP routes for the agent bounded context.

Four endpoints:
- GET  /settings/agent             → settings template (user-facing)
- POST /settings/agent/connect     → forward token to agent-server
- POST /settings/agent/disconnect  → wipe token via agent-server
- POST /api/agent/run              → SDK-backed one-shot prompt
                                     (machine-to-machine; used by the
                                      Temporal worker once its env var
                                      flips from agent-server to prep)

The /api/agent/run endpoint speaks the same wire format the agent-
server's /run does ({prompt, session_id?, resume_id?} → {stdout}),
so the worker swap is a one-line env-var change with no Go-side
diff. Auth: requires a matching PREP_INTERNAL_TOKEN header — the
worker shares the same docker network + container, but we still
gate the endpoint so it can't burn credits if accidentally exposed.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from prep import agent as _agent_mod
from prep.agent.port import AgentBudgetExhausted, AgentUnavailable
from prep.auth import current_user
from prep.web.templates import templates

logger = logging.getLogger(__name__)
router = APIRouter()


# ---- machine-to-machine /api/agent/run -------------------------------


class _RunRequest(BaseModel):
    """Wire format matching worker-go/agent.RunInput so the worker can
    point at prep with no client-side change. session_id / resume_id
    are accepted-but-ignored — the SDK port doesn't currently expose
    multi-turn sessions for our one-shot callers."""

    prompt: str
    session_id: str | None = None
    resume_id: str | None = None
    # Optional escape hatches; callers normally omit and inherit the
    # adapter's defaults (Sonnet 4.6 + medium reasoning effort).
    model: str | None = None
    reasoning: str | None = None


def _require_internal_token(x_internal_token: str | None = Header(default=None)) -> None:
    """Reject /api/agent/* calls that don't carry the shared secret.
    Configured via PREP_INTERNAL_TOKEN env var (set in deploy/*.env).
    If the env var is unset we refuse all calls — fail-closed."""
    expected = (os.environ.get("PREP_INTERNAL_TOKEN") or "").strip()
    if not expected:
        raise HTTPException(503, "PREP_INTERNAL_TOKEN not configured")
    if not x_internal_token or x_internal_token != expected:
        raise HTTPException(401, "invalid X-Internal-Token")


@router.post("/api/agent/run")
async def api_agent_run(
    body: _RunRequest,
    _gate: None = Depends(_require_internal_token),
):
    """Execute a single prompt via the SDK adapter, log usage, return
    {stdout} (matching the legacy agent-server response shape so the
    Go worker doesn't notice it's hitting a different host)."""
    adapter = _agent_mod.get_agent()
    try:
        result = await adapter.run(body.prompt, model=body.model, reasoning=body.reasoning)
    except AgentBudgetExhausted as e:
        logger.warning("agent budget exhausted: %s", e)
        # 429 maps cleanly to "you've been throttled" — workflow code
        # can distinguish from 502 to surface the budget-specific UI.
        return JSONResponse({"error": str(e), "kind": "budget_exhausted"}, status_code=429)
    except AgentUnavailable as e:
        logger.warning("agent adapter unavailable: %s", e)
        return JSONResponse({"error": str(e)}, status_code=502)

    return {"stdout": result.text}


# ---- user-facing /settings/agent --------------------------------------

# Anthropic-issued `claude setup-token` values look like
# `sk-ant-oat01-…`. Reject anything else upstream so the UI surfaces
# a clear error instead of a downstream "auth failed" later.
_TOKEN_PREFIX = "sk-ant-oat01-"


def _refresh_agent_status() -> dict:
    """Re-probe the agent and update the cached availability flag the
    template context_processor surfaces. Called after a connect or
    disconnect so the UI sees the new state on the next render."""
    s = _agent_mod.status()
    _agent_mod.set_available(bool(s.get("logged_in")))
    return s


@router.get("/settings/agent", response_class=HTMLResponse)
def settings_agent_view(request: Request, user: dict = Depends(current_user)):
    # Fold a cache refresh into the page render — whatever the live
    # status says is what the agent_available context_processor will
    # serve next, so AI-gated UI snaps to truth on the next nav.
    s = _refresh_agent_status()
    return templates.TemplateResponse(
        "settings_agent.html",
        {"request": request, "status": s, "error": None, "flash": None},
    )


@router.post("/settings/agent/connect", response_class=HTMLResponse)
async def settings_agent_connect(request: Request, user: dict = Depends(current_user)):
    """Persist a `claude setup-token` value to prep-data + activate it
    in-process. Post-SDK migration: no HTTP round-trip to a separate
    container — token storage is fully prep-side."""
    from prep.agent import token_store

    form = await request.form()
    token = (form.get("token") or "").strip()
    if not token:
        return templates.TemplateResponse(
            "settings_agent.html",
            {
                "request": request,
                "status": _agent_mod.status(),
                "error": "Token is required.",
                "flash": None,
            },
            status_code=400,
        )
    if not token.startswith(_TOKEN_PREFIX):
        return templates.TemplateResponse(
            "settings_agent.html",
            {
                "request": request,
                "status": _agent_mod.status(),
                "error": (
                    f"Token must start with {_TOKEN_PREFIX!r}. "
                    "Run `claude setup-token` on a machine you control and paste the output here."
                ),
                "flash": None,
            },
            status_code=400,
        )

    try:
        token_store.write_token(token)
    except OSError as e:
        logger.exception("token write failed")
        return templates.TemplateResponse(
            "settings_agent.html",
            {
                "request": request,
                "status": _agent_mod.status(),
                "error": f"Couldn't write token to prep-data: {e}",
                "flash": None,
            },
            status_code=500,
        )
    # Stamp into the live process env so the SDK adapter picks it up
    # without a container restart.
    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token

    s = _refresh_agent_status()
    return templates.TemplateResponse(
        "settings_agent.html",
        {
            "request": request,
            "status": s,
            "error": None,
            "flash": "Connected. AI features should be available now.",
        },
    )


@router.post("/settings/agent/disconnect", response_class=HTMLResponse)
def settings_agent_disconnect(request: Request, user: dict = Depends(current_user)):
    """Delete the persisted token + clear the process env. Idempotent
    — calling on an already-disconnected instance is a no-op."""
    from prep.agent import token_store

    token_store.delete_token()
    token_store.clear_env()
    s = _refresh_agent_status()
    return templates.TemplateResponse(
        "settings_agent.html",
        {
            "request": request,
            "status": s,
            "error": None,
            "flash": "Disconnected. AI features are now hidden; manual flows still work.",
        },
    )
