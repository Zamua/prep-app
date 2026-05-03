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
from prep import icons, notify
from prep.agent.routes import router as agent_router
from prep.auth.routes import router as auth_router
from prep.decks.routes import router as decks_router
from prep.dev import preview as dev_preview
from prep.notify.routes import router as notify_router
from prep.study.routes import router as study_router
from prep.trivia.routes import router as trivia_router
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


def _relative_time(iso_ts: str | None) -> str:
    """Jinja filter: render an ISO-8601 UTC timestamp as a coarse
    relative time ("just now", "30 min ago", "2 days ago", "3 mo ago").

    Designed for the notification log where timestamps are mostly
    < 30 days. Falls back to the raw input on parse failure so
    something visible always renders."""
    if not iso_ts:
        return ""
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(iso_ts)
    except (TypeError, ValueError):
        return iso_ts
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    secs = int((now - dt).total_seconds())
    if secs < 0:
        return "in the future"
    if secs < 45:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins} min ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours} hr ago" if hours == 1 else f"{hours} hrs ago"
    days = hours // 24
    if days < 30:
        return f"{days} day ago" if days == 1 else f"{days} days ago"
    months = days // 30
    if months < 12:
        return f"{months} mo ago"
    years = days // 365
    return f"{years} yr ago" if years == 1 else f"{years} yrs ago"


templates.env.filters["markdown"] = _markdown
templates.env.filters["relative_time"] = _relative_time
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
app.include_router(trivia_router)
app.include_router(notify_router)
app.include_router(agent_router)
app.include_router(auth_router)
app.include_router(index_router)
app.include_router(pwa_router)

# Dev-only template preview routes (read-only, no DB writes). Gated
# behind PREP_DEV — set in dev environments only, never in prod
# images. The Dockerfile.prep does not set it, so prod containers
# never expose /dev/preview/*.
if os.environ.get("PREP_DEV") == "1":
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
async def _boot() -> None:
    """Run on app boot:

    1. db.init() — schema bootstrap + idempotent migrations. Was
       removed during the DDD refactor; re-added here because
       schema changes (e.g. the trivia phase 1 ALTER TABLEs) need
       a deterministic migration trigger that isn't 'remember to
       exec into the container.'
    2. legacy push-subscriptions.json cleanup (one-time).
    3. start the notification scheduler.
    """
    from prep.infrastructure.db import init as _db_init

    _db_init()
    legacy = BASE_DIR / "push-subscriptions.json"
    if legacy.exists():
        legacy.rename(legacy.with_suffix(".json.archived-pre-v0.5"))
    notify.start_scheduler()
