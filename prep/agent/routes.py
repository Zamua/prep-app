"""HTTP routes for the agent bounded context.

Three endpoints, all under /settings/agent:
- GET  /settings/agent             → settings template
- POST /settings/agent/connect     → forward token to agent-server
- POST /settings/agent/disconnect  → wipe token via agent-server

The connect/disconnect routes call the agent-server (the docker
sidecar) over HTTP. This module is the only place outside the worker
that talks to the agent-server's /connect + /disconnect endpoints.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from prep import agent as _agent_mod
from prep.auth import current_user
from prep.web.templates import templates

router = APIRouter()


def _agent_server_url() -> str | None:
    """Returns the agent-server base URL with trailing slash trimmed,
    or None when the deploy uses a shell-agent (or no agent at all)."""
    u = (os.environ.get("PREP_AGENT_URL") or "").strip()
    return u.rstrip("/") if u else None


def _refresh_agent_status() -> dict:
    """Re-probe the agent and update the cached availability flag the
    template context_processor surfaces. Called after a connect or
    disconnect so the UI sees the new state on the next render."""
    s = _agent_mod.status()
    _agent_mod.set_available(bool(s.get("logged_in")))
    return s


@router.get("/settings/agent", response_class=HTMLResponse)
def settings_agent_view(request: Request, user: dict = Depends(current_user)):
    return templates.TemplateResponse(
        "settings_agent.html",
        {"request": request, "status": _agent_mod.status(), "error": None, "flash": None},
    )


@router.post("/settings/agent/connect", response_class=HTMLResponse)
async def settings_agent_connect(request: Request, user: dict = Depends(current_user)):
    """Forward a setup-token to the agent-server's /connect endpoint."""
    url = _agent_server_url()
    if not url:
        return templates.TemplateResponse(
            "settings_agent.html",
            {
                "request": request,
                "status": _agent_mod.status(),
                "error": (
                    "PREP_AGENT_URL is not set on this prep instance — "
                    "connect flow only applies to the docker / agent-server deploy."
                ),
                "flash": None,
            },
            status_code=400,
        )
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

    payload = json.dumps({"token": token}).encode("utf-8")
    req = urllib.request.Request(
        url + "/connect",
        data=payload,
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            err = json.loads(body).get("error") or body
        except json.JSONDecodeError:
            err = body
        return templates.TemplateResponse(
            "settings_agent.html",
            {
                "request": request,
                "status": _agent_mod.status(),
                "error": f"Agent rejected the token: {err}",
                "flash": None,
            },
            status_code=400,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return templates.TemplateResponse(
            "settings_agent.html",
            {
                "request": request,
                "status": _agent_mod.status(),
                "error": f"Couldn't reach agent-server: {e}",
                "flash": None,
            },
            status_code=502,
        )

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
    url = _agent_server_url()
    if not url:
        raise HTTPException(400, "PREP_AGENT_URL is not set on this prep instance.")
    req = urllib.request.Request(url + "/disconnect", data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return templates.TemplateResponse(
            "settings_agent.html",
            {
                "request": request,
                "status": _agent_mod.status(),
                "error": f"Couldn't reach agent-server: {e}",
                "flash": None,
            },
            status_code=502,
        )
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
