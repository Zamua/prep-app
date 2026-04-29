"""FastAPI app for prep — a self-hosted spaced-repetition flashcard tool.

This module is the bootstrap layer:
- builds the FastAPI() app with the right ROOT_PATH for the deploy
- mounts the static/ tree
- registers each bounded-context router (decks, study, notify, agent,
  auth) and the cross-cutting web routers (index, pwa)
- registers exception handlers for friendly error pages
- wires the markdown filter + icon global into the templates env
- runs the on-startup notify-scheduler boot

Per-context behaviour lives in prep/<context>/. This file should
stay short — adding more route handlers here is a smell.

The Temporal worker (worker-go/) handles long-running AI work; this
module just starts workflows + polls them. All AI calls go through
the agent-server container (see worker-go/cmd/agent-server) over HTTP.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import mistune
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from markupsafe import Markup

# Probed once at module import (cheap — file stat / one HTTP call max).
# Surfaced via the templates context_processor so AI-driven UI is
# gated everywhere the operator's deploy doesn't have an agent.
from prep import agent as _agent_mod
from prep import dev_preview, icons, notify
from prep.agent.routes import router as agent_router
from prep.auth.routes import router as auth_router
from prep.decks.routes import router as decks_router
from prep.notify.routes import router as notify_router
from prep.study.routes import router as study_router
from prep.web import errors as _errors_mod
from prep.web.index import router as index_router
from prep.web.pwa import router as pwa_router
from prep.web.templates import templates

# templates/ + static/ live at the repo root, one above the prep package.
REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_DIR = REPO_ROOT
ROOT_PATH = os.environ.get("ROOT_PATH", "")

_log = logging.getLogger("prep")

# Boot-time agent probe so the templates context_processor + AI-gating
# route guards share one source of truth.
_agent_mod.init_availability()

# Markdown rendering for prompts + free-form fields. mistune escapes
# raw HTML by default; input is already trusted (we generated it
# ourselves) but we still want **bold** / `code` / fenced blocks /
# lists / headings to render rather than show as raw markdown text.
_md = mistune.create_markdown(
    escape=True,
    hard_wrap=False,
    plugins=["strikethrough", "table"],
)


def _markdown(text: str | None) -> Markup:
    """Jinja filter: render markdown to safe HTML. Returns empty
    string for None so templates can `{{ q.prompt|markdown }}` without
    guards."""
    if not text:
        return Markup("")
    return Markup(_md(text))


templates.env.filters["markdown"] = _markdown
templates.env.globals["icon"] = icons.icon

# ---- App + mounts ---------------------------------------------------------

app = FastAPI(root_path=ROOT_PATH)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

_errors_mod.register(app)

# Bounded-context routers. Each per-context module owns the HTTP
# surface for its slice; this file just wires them up. No route
# handlers should live here.
app.include_router(decks_router)
app.include_router(study_router)
app.include_router(notify_router)
app.include_router(agent_router)
app.include_router(auth_router)
app.include_router(index_router)
app.include_router(pwa_router)

# Dev-only template preview routes (read-only, no DB writes).
dev_preview.register(app, templates)

# ---- Boot logging ---------------------------------------------------------

_default_user_at_boot = os.environ.get("PREP_DEFAULT_USER")
if _default_user_at_boot:
    _log.info(
        "PREP_DEFAULT_USER=%s — every header-less request will be authenticated as this user. "
        "Fine for local dev; remove in prod unless you really want a single-user shared identity.",
        _default_user_at_boot,
    )

if _agent_mod.is_available:
    _log.info("agent: AI features ENABLED (PREP_AGENT_URL or PREP_AGENT_BIN reachable).")
else:
    _log.info(
        "agent: AI features DISABLED — no PREP_AGENT_URL set, and PREP_AGENT_BIN "
        "(default ~/.local/bin/claude) doesn't exist. Manual flashcard mode only."
    )


# ---- Startup hooks --------------------------------------------------------


@app.on_event("startup")
async def _notify_boot() -> None:
    """Move the staging-experiment subscription file out of the way
    (one-time legacy cleanup) and start the notification scheduler.
    Idempotent: a second start_scheduler() call is a no-op."""
    legacy = BASE_DIR / "push-subscriptions.json"
    if legacy.exists():
        legacy.rename(legacy.with_suffix(".json.archived-pre-v0.5"))
    notify.start_scheduler()
