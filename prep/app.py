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
from prep import workflows as _workflows_mod
from prep.agent.routes import router as agent_router
from prep.api.routes import router as api_router
from prep.auth.routes import router as auth_router
from prep.decks.routes import router as decks_router
from prep.dev import preview as dev_preview
from prep.notify.routes import router as notify_router
from prep.study.routes import router as study_router
from prep.trivia.routes import router as trivia_router
from prep.web import errors as _errors_mod
from prep.web.index import router as index_router
from prep.web.legal import router as legal_router
from prep.web.pwa import router as pwa_router
from prep.web.templates import templates
from prep.workflows.routes import router as workflows_router

# templates/ + static/ live at the repo root, one above the prep package.
REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_DIR = REPO_ROOT
ROOT_PATH = os.environ.get("ROOT_PATH", "")

# Configure the `prep` logger tree so info-level diagnostics actually
# reach stdout (uvicorn doesn't auto-attach handlers to app loggers).
# Targeted to the "prep" namespace so we don't loosen uvicorn's own
# logging or root-handler config. Each module's `logging.getLogger(__name__)`
# inherits from this. Format is plain so it composes with goreman's
# per-process prefix (`[36m18:30:08      app | [m...`).
_PREP_LOG_LEVEL = os.environ.get("PREP_LOG_LEVEL", "INFO").upper()
_log = logging.getLogger("prep")
if not _log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _log.addHandler(_h)
    _log.propagate = False
_log.setLevel(_PREP_LOG_LEVEL)

# Defense-in-depth: scrub Anthropic OAuth tokens + API keys from every
# log line our logger emits. We don't *intentionally* log secrets, but
# accident routes exist (exception traces echoing a request body, a
# library debug log that includes headers). See prep/web/log_redaction.py.
from prep.web.log_redaction import install_on as _install_redaction

_install_redaction(_log)
# uvicorn's own loggers handle the request path (`--no-access-log` is
# on in prod so they're mostly quiet, but error-level lines from
# uvicorn.error still flow to stdout). Wrap them too — same accident
# routes apply.
_install_redaction(logging.getLogger("uvicorn"))
_install_redaction(logging.getLogger("uvicorn.error"))

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


def _wakes_in(iso_ts: str | None) -> str:
    """Jinja filter: render a FUTURE ISO-8601 UTC timestamp as the
    delta from now ("in 45 min" / "in 3 hrs" / "tomorrow" / "in 4
    days"). Used by the Snoozed sub-section to show when each
    snoozed session will resurface. Past timestamps (already woken)
    surface as the empty string so the template can skip them.

    The "forever" snooze preset maps to a year-2099 sentinel (see
    prep.web.durations.FOREVER_ISO) so the read path doesn't have to
    special-case None vs forever everywhere. A literal arithmetic
    render of that ("in 73 years") is silly — anything past ~5 years
    is effectively forever in app terms, so we collapse it."""
    if not iso_ts:
        return ""
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(iso_ts)
    except (TypeError, ValueError):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = int((dt - datetime.now(timezone.utc)).total_seconds())
    if secs <= 0:
        return ""
    # ~5 year cap before we collapse to "forever". Below that we render
    # a real delta; above, anything is forever-shaped.
    if secs > 5 * 365 * 86400:
        return "forever"
    if secs < 60:
        return "in <1 min"
    # Round to the nearest unit so picking "1 day" and reloading the
    # page a second later renders "in 1 day", not "in 23 hrs".
    mins = (secs + 30) // 60
    if mins < 60:
        return f"in {mins} min"
    hours = (mins + 30) // 60
    if hours < 24:
        return "in 1 hr" if hours == 1 else f"in {hours} hrs"
    days = (hours + 12) // 24
    if days == 1:
        return "tomorrow"
    if days < 30:
        return f"in {days} days"
    months = days // 30
    if months < 12:
        return "next month" if months == 1 else f"in {months} months"
    years = days // 365
    return "next year" if years == 1 else f"in {years} years"


templates.env.filters["markdown"] = _markdown
templates.env.filters["relative_time"] = _relative_time
templates.env.filters["wakes_in"] = _wakes_in
templates.env.globals["icon"] = icons.icon

# ---- App + mounts ---------------------------------------------------------

app = FastAPI(root_path=ROOT_PATH)


# Prometheus metrics. Middleware records per-request latency; the
# /metrics route exposes the registry to the obs-stack scraper.
# Registered BEFORE the routers so every routed request flows through
# the timing middleware. See prep/web/metrics.py for the four signals.
from prep.web import metrics as _metrics  # noqa: E402

app.middleware("http")(_metrics.http_metrics_middleware)


# Force HTML + manifest responses to re-validate on every navigation.
# Hashed asset URLs (CSS `?v=…`, versioned JS module space) already
# defeat caching for static files — but only if the HTML pointing at
# them is fresh. iOS PWA standalone aggressively caches the start_url
# HTML in its Web App Bundle, so without no-cache on the HTML the
# installed PWA serves the previous deploy's `?v=` token forever and
# never picks up new CSS / JS. Symptom: post-deploy layout glitches
# that only repro inside the home-screen PWA, not in Safari proper.
# Same fix nginx's `expires -1` does for `index.html` in classic SPA
# hosting — we apply it at the app layer because we don't have a
# reverse-proxy hop that owns it.
@app.middleware("http")
async def _no_cache_html(request, call_next):
    response = await call_next(request)
    ct = response.headers.get("content-type", "")
    path = request.url.path
    if ct.startswith("text/html") or path.endswith("/manifest.json"):
        response.headers["cache-control"] = "no-cache, no-store, must-revalidate"
    return response


@app.get("/metrics", include_in_schema=False)
async def metrics_endpoint():
    """Prometheus scrape target. Plain-text exposition format."""
    return await _metrics.metrics_response()


# Versioned ES-module URL space. The importmap in base.html resolves
# `@/` to `/static/js/v<build>/`, so the URL of every imported module
# changes on every deploy — the canonical "hashed asset" caching
# pattern, just with the version applied to the URL prefix instead of
# per-file. This is what bundlers (webpack/rollup/vite) do via
# content-hashed filenames; without a bundler we keep the on-disk
# layout flat and rewrite the URL here. The version segment is
# discarded — it's only there to produce a fresh URL.
#
# Why: ES modules under an importmap have no spec-compliant way to
# carry a `?v=` cache-buster on resolved imports, so without the
# versioned URL space, browsers (notably iOS PWA standalone) hold
# the prior deploy's bytes indefinitely. Versioned URLs + immutable
# cache headers are the standard solution: every deploy gets a new
# URL, every URL caches forever.
@app.get("/static/js/v{build}/{path:path}")
def _versioned_js(build: str, path: str):
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    # FastAPI's `v{build}` path-param is greedy on any string after
    # `/static/js/v`, so this route also catches `/static/js/vendor/…`
    # (build="endor"), `/static/js/version.txt` etc. Disambiguate:
    # if `build` looks like our timestamp, strip it and serve from
    # static/js/{path} with immutable cache. Otherwise treat the
    # whole `v{build}/{path}` as the literal sub-path under static/js
    # (no version stripping, no immutable cache — same handling the
    # StaticFiles mount would have given it).
    is_versioned = build.isdigit()
    if is_versioned:
        rel = path
    else:
        rel = f"v{build}/{path}"
    target = (BASE_DIR / "static" / "js" / rel).resolve()
    js_root = (BASE_DIR / "static" / "js").resolve()
    if js_root not in target.parents and target != js_root:
        raise HTTPException(status_code=404)
    if not target.is_file():
        raise HTTPException(status_code=404)
    headers = {"cache-control": "public, max-age=31536000, immutable"} if is_versioned else {}
    return FileResponse(target, media_type="application/javascript", headers=headers)


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
app.include_router(api_router)
app.include_router(auth_router)
app.include_router(index_router)
app.include_router(legal_router)
app.include_router(pwa_router)
app.include_router(workflows_router)

# Clerk webhook receiver — mounted only when configured so the
# import of `clerk-backend-api` / `svix` doesn't happen on
# Tailscale-mode deploys (where the env var is absent). The route
# itself defensive-checks the env var too; this gate is the cheap
# import-cost optimization.
if os.environ.get("CLERK_WEBHOOK_SECRET"):
    from prep.auth.webhooks_clerk import router as clerk_webhook_router

    app.include_router(clerk_webhook_router)

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
    4. start the workflow reconciler — the server-side fallback that
       keeps active_workflows accurate when the user isn't polling.
       Same shape as notify.start_scheduler: one bg task per app.
    """
    from prep.infrastructure.db import init as _db_init

    _db_init()
    legacy = BASE_DIR / "push-subscriptions.json"
    if legacy.exists():
        legacy.rename(legacy.with_suffix(".json.archived-pre-v0.5"))
    notify.start_scheduler()
    _workflows_mod.start_workflows_scheduler()
