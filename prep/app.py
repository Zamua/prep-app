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
the in-process `claude-agent-sdk` adapter (prep.agent.sdk_adapter);
the Go worker POSTs `/api/agent/run` against its own host to invoke
it. The retired sidecar container (worker-go/cmd/agent-server) was
removed during the SDK migration.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import mistune
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
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
from prep.auth.anon_cookie import emit_cookie_updates as emit_anon_cookie_updates
from prep.auth.routes import router as auth_router
from prep.decks.routes import router as decks_router
from prep.dev import preview as dev_preview
from prep.infrastructure import clock
from prep.instant.routes import router as instant_router
from prep.notify.routes import router as notify_router
from prep.offline.routes import router as offline_router
from prep.study.api import router as study_api_router
from prep.study.routes import router as study_router
from prep.trivia.routes import router as trivia_router
from prep.web import errors as _errors_mod
from prep.web.dashboard import router as dashboard_router
from prep.web.index import router as index_router
from prep.web.legal import router as legal_router
from prep.web.parity import parity_mode, strip_cross_origin_tags
from prep.web.parity import router as parity_router
from prep.web.pwa import router as pwa_router
from prep.web.templates import is_accepted_version_token, templates
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


def _warn_on_unusable_master_key() -> None:
    """Say at boot when PREP_KEY_ENCRYPTION_SECRET is set but unusable.

    Without this the deploy looks healthy and BYOK simply never works:
    the failure surfaces only when a user tries to save a key, and the
    anonymous-cookie secret derived from it silently stays off. A
    misconfigured value is worth one loud line at startup. Unset is a
    valid deploy shape (no BYOK) and stays quiet."""
    import os as _os

    raw = (_os.environ.get("PREP_KEY_ENCRYPTION_SECRET") or "").strip()
    if not raw:
        return
    from prep.byok.crypto import MasterKeyError, load_master_from_env

    try:
        load_master_from_env("PREP_KEY_ENCRYPTION_SECRET")
    except MasterKeyError as e:
        _log.error(
            "PREP_KEY_ENCRYPTION_SECRET is set but unusable, so saving a BYOK "
            "key will fail on this deploy: %s. It must be hex, as from "
            "`openssl rand -hex 32`; a base64 value is the usual mistake.",
            e,
        )


def _warn_on_parity_mode() -> None:
    """Parity mode drops ClerkJS from every page; a deploy carrying
    the flag would sign nobody in."""
    if parity_mode():
        _log.warning("PREP_PARITY_MODE=1: ClerkJS and the vendor doc scripts are off")


_warn_on_unusable_master_key()
_warn_on_parity_mode()

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
    now = clock.now()
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
    secs = int((dt - clock.now()).total_seconds())
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


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Startup: db.init() (schema bootstrap + idempotent migrations,
    the deterministic migration trigger for schema changes), one-time
    legacy push-subscriptions.json cleanup, then the notification and
    workflow-reconciler background schedulers (one bg task each)."""
    from prep.infrastructure.db import init as _db_init

    _db_init()
    legacy = BASE_DIR / "push-subscriptions.json"
    if legacy.exists():
        legacy.rename(legacy.with_suffix(".json.archived-pre-v0.5"))
    notify.start_scheduler()
    _workflows_mod.start_workflows_scheduler()
    yield


# FastAPI's auto-docs at /docs (Swagger) + /redoc are publicly visible
# at the deploy's root_path. The title/description below drives the
# header users see when they hit those URLs, and the version comes
# from the deployed image. The tag list groups the schema's routes:
# "Public API" + "MCP" surfaces (the part agents/scripts care about)
# sort to the top; everything else is internal.
app = FastAPI(
    root_path=ROOT_PATH,
    lifespan=_lifespan,
    # Disable FastAPI's auto-mounted /redoc — its default CDN URL
    # uses `redoc@next` which Chromium blocks via Opaque Response
    # Blocking on prepcards.app (the jsdelivr response Content-Type
    # for the @next tag confuses ORB). We mount our own /redoc below
    # pinned to a stable version. /docs is mounted below too, the
    # FastAPI default shell, so parity mode can strip its CDN tags.
    redoc_url=None,
    docs_url=None,
    title="prep",
    description=(
        "Self-hosted spaced-repetition flashcards.\n\n"
        "This document covers the **public REST API** at `/api/v1/*` and "
        "the **MCP (Model Context Protocol) server** at `/mcp`. Both "
        "share the same bearer-token auth — mint a token at "
        "`/settings/api` and pass it as `Authorization: Bearer prep_pat_…`.\n\n"
        "Source: https://github.com/Zamua/prep-app — "
        "AI-agent manifest: [/llms.txt](/llms.txt)"
    ),
    version="1.0.0",
    openapi_tags=[
        {"name": "Decks API", "description": "Public REST surface for managing decks + cards."},
        {"name": "MCP", "description": "Model Context Protocol server. JSON-RPC 2.0 over HTTP."},
    ],
)


# Prometheus metrics. Middleware records per-request latency; the
# /metrics route exposes the registry to the obs-stack scraper.
# Registered BEFORE the routers so every routed request flows through
# the timing middleware. See prep/web/metrics.py for the signals.
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
    # Anonymous-cookie side effects the resolver could only record,
    # having no response of its own: clear a dead cookie, re-mint an
    # aging one. Outside the content-type check below, or a JSON
    # response never clears and never refreshes.
    emit_anon_cookie_updates(request, response)
    ct = response.headers.get("content-type", "")
    path = request.url.path
    if ct.startswith("text/html") or path.endswith("/manifest.json"):
        response.headers["cache-control"] = "no-cache, no-store, must-revalidate"
    return response


def _vendor_shell(request: Request, response: HTMLResponse) -> HTMLResponse:
    """Under parity mode the doc shells lose every cross-origin script
    and stylesheet; the gate compares the empty shells."""
    if not parity_mode():
        return response
    body = response.body.decode(response.charset)
    host = request.headers.get("host", "")
    return HTMLResponse(strip_cross_origin_tags(body, host), status_code=response.status_code)


@app.get("/docs", include_in_schema=False)
def custom_docs(request: Request):
    """FastAPI's own Swagger UI shell, mounted by hand so it passes
    through `_vendor_shell`."""
    from fastapi.openapi.docs import get_swagger_ui_html

    root = request.scope.get("root_path", "").rstrip("/")
    oauth2_redirect_url = app.swagger_ui_oauth2_redirect_url
    if oauth2_redirect_url:
        oauth2_redirect_url = root + oauth2_redirect_url
    return _vendor_shell(
        request,
        get_swagger_ui_html(
            openapi_url=root + app.openapi_url,
            title=f"{app.title} - Swagger UI",
            oauth2_redirect_url=oauth2_redirect_url,
            init_oauth=app.swagger_ui_init_oauth,
            swagger_ui_parameters=app.swagger_ui_parameters,
        ),
    )


@app.get(app.swagger_ui_oauth2_redirect_url, include_in_schema=False)
def custom_docs_oauth2_redirect():
    from fastapi.openapi.docs import get_swagger_ui_oauth2_redirect_html

    return get_swagger_ui_oauth2_redirect_html()


@app.get("/redoc", include_in_schema=False)
def custom_redoc(request: Request):
    """Replacement for FastAPI's auto-mounted /redoc.

    Two reasons for the override:
    1. Pin the redoc bundle (the default `@next` tag served by
       jsdelivr trips Chromium's Opaque Response Blocking).
    2. Prepend root_path to the openapi_url. FastAPI's
       `app.openapi_url` is `/openapi.json` — Swagger UI rewrites
       this automatically based on `<base>` / root_path, but ReDoc
       takes the value literally → 404 under a deploy that mounts at
       `/prep-staging/` or `/prep/`."""
    from fastapi.openapi.docs import get_redoc_html

    root = request.scope.get("root_path", "")
    openapi_url = f"{root}{app.openapi_url}"
    return _vendor_shell(
        request,
        get_redoc_html(
            openapi_url=openapi_url,
            title=f"{app.title} - ReDoc",
            redoc_js_url="https://cdn.jsdelivr.net/npm/redoc@2.1.5/bundles/redoc.standalone.js",
        ),
    )


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
    # if `build` looks like a build token (the lowercase-hex shape,
    # or the legacy all-digit boot stamps pre-offline pages still
    # reference), strip it and serve from static/js/{path} with
    # immutable cache. The token is opaque: ANY accepted value serves
    # the current build's bytes, which is what lets a page from the
    # previous build keep resolving assets across a deploy. Otherwise
    # treat the whole `v{build}/{path}` as the literal sub-path under
    # static/js (no version stripping, no immutable cache; the same
    # handling the StaticFiles mount would have given it).
    is_versioned = is_accepted_version_token(build)
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


# Versioned CSS URL space — same pattern as the JS route above, for
# the same reason: iOS Safari (and the PWA standalone variant in
# particular) treats query-only cache busts inconsistently. The
# pre-existing `?v={{ static_css_mtime }}` on the CSS <link> works
# for chromium and webkit but isn't reliable on iOS — after a deploy,
# users had to private-tab or pull-to-refresh to see fresh CSS.
#
# A versioned PATH (`/static/css/v<build>/index.css`) is a different
# URL by Safari's cache-key, full stop. Browsers fetch it fresh
# every deploy without any per-user dance. CSS @import statements
# inside index.css use relative URLs (`./components/foo.css`), which
# the browser resolves to `/static/css/v<build>/components/foo.css`
# — also handled by this route.
@app.get("/static/css/v{build}/{path:path}")
def _versioned_css(build: str, path: str):
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    # Same token-acceptance rule as the JS route above: current hex
    # tokens or legacy digit stamps strip-and-serve with immutable
    # cache; anything else is a literal sub-path.
    is_versioned = is_accepted_version_token(build)
    if is_versioned:
        rel = path
    else:
        rel = f"v{build}/{path}"
    target = (BASE_DIR / "static" / "css" / rel).resolve()
    css_root = (BASE_DIR / "static" / "css").resolve()
    if css_root not in target.parents and target != css_root:
        raise HTTPException(status_code=404)
    if not target.is_file():
        raise HTTPException(status_code=404)
    headers = {"cache-control": "public, max-age=31536000, immutable"} if is_versioned else {}
    return FileResponse(target, media_type="text/css", headers=headers)


class _RevalidatingStaticFiles(StaticFiles):
    """StaticFiles with `Cache-Control: no-cache` on every response.

    Without this header, browsers (notably iOS Safari) fall back to
    heuristic caching for `/static/*` — they reuse the prior copy of
    `index.css` for a guess-based interval after a deploy, so the user
    sees stale CSS even though the new bytes are sitting on disk.

    `no-cache` doesn't mean "don't cache" — it means "revalidate via
    etag/last-modified before reusing." Starlette's StaticFiles emits
    strong etags, so unchanged files come back as 304 (no body) and
    changed files come back as 200 with the fresh bytes. Both round
    trips are tiny on a tailnet/Clerk-hosted single-region deploy.

    Hashed assets (the `/static/js/v<build>/...` route above) bypass
    this mount entirely and serve their own `immutable` Cache-Control.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", _RevalidatingStaticFiles(directory=str(BASE_DIR / "static")), name="static")

_errors_mod.register(app)

# Bounded-context routers. Each per-context module owns the HTTP
# surface for its slice; this file just wires them up. No route
# handlers should live here.
app.include_router(decks_router)
app.include_router(study_router)
app.include_router(study_api_router)
app.include_router(trivia_router)
app.include_router(notify_router)
app.include_router(agent_router)
app.include_router(api_router)
app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(index_router)
app.include_router(instant_router)
app.include_router(legal_router)
app.include_router(offline_router)
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

if parity_mode():
    app.include_router(parity_router)


# ---- Boot logging ---------------------------------------------------------

_default_user_at_boot = os.environ.get("PREP_DEFAULT_USER")
if _default_user_at_boot:
    _log.info(
        "PREP_DEFAULT_USER=%s — every header-less request will be authenticated as this user. "
        "Fine for local dev; remove in prod unless you really want a single-user shared identity.",
        _default_user_at_boot,
    )

if _agent_mod.is_available:
    _log.info(
        "agent: deploy-wide CLAUDE_CODE_OAUTH_TOKEN is set (single-user / fallback path active)."
    )
else:
    _log.info(
        "agent: no deploy-wide token. AI features are per-user — each user "
        "configures their own BYOK key at /settings/agent (or, on tailscale "
        "single-user installs, runs `claude setup-token` and pastes it)."
    )
