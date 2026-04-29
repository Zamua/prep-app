"""FastAPI app for prep — a self-hosted spaced-repetition flashcard tool.

Surfaces:
  - Deck index, deck view, manual + AI card creation flows
  - Study sessions (cross-device, version-checked) with SRS advancement
  - Plan-first generation (claude outline → review/replan/accept → expand)
  - Transform (claude rewrites cards / decks; preview before apply)
  - Web push notifications (VAPID), PWA manifest + service worker
  - Settings: editor input mode, AI agent connect, notification prefs

The Temporal worker (worker-go/) handles long-running AI work; this
module just starts workflows + polls them. All AI calls go through the
agent-server container (see worker-go/cmd/agent-server) over HTTP.
"""

from __future__ import annotations

import json
from pathlib import Path

import mistune
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from markupsafe import Markup
from starlette.exceptions import HTTPException as StarletteHTTPException

# Probed once at module import (cheap — file stat / one HTTP call max).
# Surfaced via _agent_context so templates can gate AI-driven UI.
from prep import agent as _agent_mod
from prep import (
    db,
    icons,
    notify,
)

# REPO_ROOT (not BASE_DIR) — the package now lives one level below the
# repo root, but `templates/` and `static/` stay at the repo root so
# they aren't shipped into the python package's import surface.
REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_DIR = REPO_ROOT

# Cache the agent availability probe in the agent module so context
# processors + AI-gating route guards share one source of truth. The
# /settings/agent/connect route mutates it after a fresh token is
# pasted via prep.agent.set_available().
_agent_mod.init_availability()

# Templates instance lives in prep/web/templates.py so per-context
# routers can render templates without cycling back through app.py.
from prep.web.templates import templates  # noqa: E402,I001 — placement matters

# Markdown rendering for prompts (and other free-form fields). Mistune escapes
# raw HTML by default — input is already trusted (we generated it ourselves)
# but we still want **bold** / `code` / fenced blocks / lists / headings to
# render rather than show as raw markdown text.
_md = mistune.create_markdown(
    escape=True,  # escape any raw HTML; we don't want pass-through
    hard_wrap=False,
    plugins=["strikethrough", "table"],
)


def _markdown(text: str | None) -> Markup:
    """Jinja filter: render markdown to safe HTML. Returns empty string for
    None so templates can `{{ q.prompt|markdown }}` without guards."""
    if not text:
        return Markup("")
    return Markup(_md(text))


templates.env.filters["markdown"] = _markdown
templates.env.globals["icon"] = icons.icon

# When fronted by Caddy at a path prefix, set ROOT_PATH so generated URLs include it.
import os

ROOT_PATH = os.environ.get("ROOT_PATH", "")

app = FastAPI(root_path=ROOT_PATH)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ---- Friendly error pages -------------------------------------------------
#
# FastAPI's default for any HTTPException is `{"detail": "..."}` JSON, which
# renders raw in a browser tab. For HTML clients we replace that with a
# literary-styled error page that explains what happened and offers a way
# back home. JSON clients (anyone who sets Accept: application/json or hits
# a /notify/* endpoint that returns JSON-shaped data) keep the bare detail
# response so callers can parse the error programmatically.

_ERROR_COPY = {
    400: ("Bad request.", "Something in that URL didn't quite parse."),
    401: (
        "Not signed in.",
        "prep authenticates via Tailscale Serve — open this page through your tailnet so the server can read your Tailscale identity. "
        "For local development, set PREP_DEFAULT_USER (the make dev shim does this automatically).",
    ),
    403: ("Forbidden.", "That's not yours to look at."),
    404: (
        "Not found.",
        "We couldn't find what you were looking for. Maybe a typo, or the link is stale.",
    ),
    409: ("Out of date.", "Something changed since this page loaded. Reload and try again."),
    422: ("Bad input.", "The form didn't validate. Go back and try again."),
    500: ("Something broke.", "Sorry — that's on our end. The error has been logged."),
}


def _wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    if "application/json" in accept and "text/html" not in accept:
        return True
    # Any /notify/* JSON endpoint should not get an HTML error page on its
    # POST responses — the JS code on the demo / settings page expects JSON.
    path = request.url.path
    if (
        path.endswith("/subscribe")
        or path.endswith("/unsubscribe")
        or path.endswith("/test")
        or path.endswith("/prefs")
        or path.endswith("/vapid-public-key")
    ):
        return True
    return False


def _render_error(request: Request, status_code: int, detail: str | None = None):
    headline, blurb = _ERROR_COPY.get(
        status_code,
        ("Something went sideways.", "An unexpected error happened. The team has been notified."),
    )
    if detail and detail != headline:
        # Fold the original detail into the blurb so we don't lose
        # context (e.g., "malformed workflow id" vs. generic "Bad request").
        blurb = f"{blurb} ({detail})"
    return templates.TemplateResponse(
        "error.html",
        {
            "request": request,
            "status_code": status_code,
            "headline": headline,
            "blurb": blurb,
            "path": request.url.path,
        },
        status_code=status_code,
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if _wants_json(request):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return _render_error(request, exc.status_code, exc.detail)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    if _wants_json(request):
        return JSONResponse({"detail": exc.errors()}, status_code=422)
    return _render_error(request, 422, "Form did not validate.")


@app.exception_handler(Exception)
async def server_exception_handler(request: Request, exc: Exception):
    import logging
    import traceback

    logging.getLogger("prep").error(
        "unhandled exception on %s %s: %s\n%s",
        request.method,
        request.url.path,
        exc,
        traceback.format_exc(),
    )
    if _wants_json(request):
        return JSONResponse({"detail": "internal server error"}, status_code=500)
    return _render_error(request, 500)


# Backwards-compat alias for the in-app routes that haven't migrated to
# the per-context routers yet. New code imports from prep.web.responses.


# ---- Auth dependency -------------------------------------------------------
#
# Every authenticated route depends on `current_user`. We resolve identity in
# this order:
#   1. `Tailscale-User-Login` header (set by `tailscale serve` when properly
#      configured to forward identity headers — not yet wired up; placeholder
#      for the post-v0.3.0 plumbing).
#   2. `PREP_DEFAULT_USER` env var — the inviting user. This covers the
#      common case where the tailnet has only one identity (the owner) and
#      removes the need for header plumbing for solo deployments.
#   3. None → unauthenticated. (For now this raises 401; once we add the
#      landing page in a later release this becomes a redirect.)
#
# `current_user` upserts the user row and returns the dict. This is cheap
# (an upsert on every request) but keeps user.last_seen_at fresh.

from fastapi import Depends

# Auth dependency lives in prep.auth so other routers (decks, study,
# etc.) can import it without cycling back through app.py. The shape
# is unchanged from when it was inlined here.
from prep.auth import current_user

db.init()
# No boot-seed: deck rows materialize per-user the first time they
# navigate to /deck/{name} (or hit any route that calls
# get_or_create_deck). v0.3.2 dropped a previous startup-seed block
# that auto-created an "owner@local" placeholder user, leaving dev
# fixtures in prod tables on every restart.

import logging

_log = logging.getLogger("prep")

# Surface PREP_DEFAULT_USER state at boot so an operator who's
# accidentally left it set in prod sees it loudly. Useful as well
# for `make dev` so the contributor knows auth is being bypassed.
_default_user_at_boot = os.environ.get("PREP_DEFAULT_USER")
if _default_user_at_boot:
    _log.info(
        "PREP_DEFAULT_USER=%s — every header-less request will be authenticated as this user. "
        "Fine for local dev; remove in prod unless you really want a single-user shared identity.",
        _default_user_at_boot,
    )

# Surface agent availability at boot so the operator can tell whether AI
# features will work without checking the UI. _AGENT_AVAILABLE was probed
# at module import; we just log it here.
if _agent_mod.is_available:
    _log.info("agent: AI features ENABLED (PREP_AGENT_URL or PREP_AGENT_BIN reachable).")
else:
    _log.info(
        "agent: AI features DISABLED — no PREP_AGENT_URL set, and PREP_AGENT_BIN "
        "(default ~/.local/bin/claude) doesn't exist. Manual flashcard mode only."
    )

# Bounded-context routers. Each per-context module owns the HTTP
# surface for its slice; routes call into the context's service layer
# (which holds repos + temporal-client orchestration). app.py just
# mounts them — no decks/study/notify route-handler code lives here
# anymore (or won't, by the end of phase 5+).
from prep.decks.routes import router as decks_router
from prep.study.routes import router as study_router

app.include_router(decks_router)
app.include_router(study_router)

# Dev-only template preview routes for the UI sweep — read-only, no DB writes.
from prep import dev_preview

dev_preview.register(app, templates)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, user: dict = Depends(current_user)):
    uid = user["tailscale_login"]
    # All decks live in the DB now (created via /decks/new or the legacy
    # bootstrap-seed path that ran on early v0.x deploys). No more source-
    # code DECK_CONTEXT catalog merging.
    decks = sorted(db.list_decks(uid), key=lambda d: d["name"])
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "user": user,
            "decks": decks,
            "recent_sessions": db.list_recent_sessions(uid, limit=5),
        },
    )


# ---- Deck creation (UI-driven) -------------------------------------------

# /decks/new (GET + POST) and /deck/{name} moved to prep.decks.routes.


# ---- PWA install (manifest + service worker) ------------------------------
#
# These two routes are intentionally NOT gated by current_user: the manifest
# and service worker have to be reachable before the PWA is "installed", and
# the install-from-Safari flow doesn't reliably carry Tailscale-User-Login
# on its first hit. Auth kicks in for any actual app view the moment the
# PWA navigates into one.


@app.get("/manifest.json")
def manifest(request: Request) -> JSONResponse:
    """Web App Manifest, dynamic so the scope/start_url match whatever
    ROOT_PATH this instance is served at (so prep vs prep-staging both
    install correctly without a hand-edited manifest each time)."""
    root = ROOT_PATH or ""
    env_label = "staging" if "staging" in root else ""
    short = "prep" + (f" ({env_label})" if env_label else "")
    return JSONResponse(
        {
            "name": f"prep · a commonplace book{' (staging)' if env_label else ''}",
            "short_name": short,
            "description": "Spaced-repetition flashcards. Learn anything.",
            "display": "standalone",
            "scope": (root + "/") or "/",
            "start_url": (root + "/") or "/",
            "background_color": "#f4ecdc",
            "theme_color": "#f5efe6",
            "icons": [
                {"src": f"{root}/static/pwa/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": f"{root}/static/pwa/icon-512.png", "sizes": "512x512", "type": "image/png"},
            ],
        }
    )


@app.get("/sw.js")
def service_worker():
    """Serve the SW from the app's root scope (rather than /static/sw.js
    whose default scope is /static/). The browser uses the SW's URL path
    as its scope, so this URL is what determines what the SW controls."""
    return FileResponse(BASE_DIR / "static" / "sw.js", media_type="application/javascript")


# ---- Editor settings ------------------------------------------------------


@app.get("/settings/editor", response_class=HTMLResponse)
def editor_settings(request: Request, user: dict = Depends(current_user)):
    return templates.TemplateResponse(
        "settings_editor.html",
        {
            "request": request,
            "user": user,
            "current_mode": db.get_editor_input_mode(user["tailscale_login"]),
            "modes": db.EDITOR_INPUT_MODES,
            "saved": False,
        },
    )


@app.post("/settings/editor", response_class=HTMLResponse)
def editor_settings_save(
    request: Request, mode: str = Form(...), user: dict = Depends(current_user)
):
    if mode not in db.EDITOR_INPUT_MODES:
        raise HTTPException(400, f'Unknown input mode "{mode}".')
    db.set_editor_input_mode(user["tailscale_login"], mode)
    return templates.TemplateResponse(
        "settings_editor.html",
        {
            "request": request,
            "user": {**user, "editor_input_mode": mode},  # reflect saved value in next render
            "current_mode": mode,
            "modes": db.EDITOR_INPUT_MODES,
            "saved": True,
        },
    )


# ---- AI agent settings + connect/disconnect -------------------------------
#
# When PREP_AGENT_URL is set (the docker / agent-server deploy shape),
# this page drives the connect flow: user pastes a `claude setup-token`
# OAuth token, we forward it to the agent-server's POST /connect, which
# persists it in its volume. Subsequent /run calls inject the token as
# CLAUDE_CODE_OAUTH_TOKEN env var.
#
# When PREP_AGENT_BIN is set instead (legacy shell-agent deploy), the
# page just shows status — auth is managed by the host claude CLI.


def _agent_server_url() -> str | None:
    u = (os.environ.get("PREP_AGENT_URL") or "").strip()
    return u.rstrip("/") if u else None


def _refresh_agent_status() -> dict:
    """Re-probe the agent and update the cached availability flag the
    template context_processor surfaces. Called after a connect/disconnect
    so the UI sees the new state immediately."""
    s = _agent_mod.status()
    _agent_mod.set_available(bool(s.get("logged_in")))
    return s


@app.get("/settings/agent", response_class=HTMLResponse)
def settings_agent_view(request: Request, user: dict = Depends(current_user)):
    s = _agent_mod.status()
    return templates.TemplateResponse(
        "settings_agent.html",
        {"request": request, "status": s, "error": None, "flash": None},
    )


@app.post("/settings/agent/connect", response_class=HTMLResponse)
async def settings_agent_connect(request: Request, user: dict = Depends(current_user)):
    """Forward a setup-token to the agent-server's /connect endpoint."""
    url = _agent_server_url()
    if not url:
        # Shell-agent deploy or no agent at all — there's nothing for us
        # to connect to. The UI hides this form in that case but defend
        # against direct posts.
        return templates.TemplateResponse(
            "settings_agent.html",
            {
                "request": request,
                "status": _agent_mod.status(),
                "error": "PREP_AGENT_URL is not set on this prep instance — connect flow only applies to the docker / agent-server deploy.",
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

    # Forward to agent-server. Use urllib (already used for the probe);
    # avoids pulling in httpx for one call.
    import urllib.error
    import urllib.request

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


@app.post("/settings/agent/disconnect", response_class=HTMLResponse)
def settings_agent_disconnect(request: Request, user: dict = Depends(current_user)):
    url = _agent_server_url()
    if not url:
        raise HTTPException(400, "PREP_AGENT_URL is not set on this prep instance.")
    import urllib.error
    import urllib.request

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


# ---- Notifications (settings + subscribe) ---------------------------------

_VALID_MODES = {"off", "digest", "when-ready"}


def _validate_prefs(p: dict) -> dict:
    """Validate + clamp incoming preference values. Anything missing or
    out-of-range falls back to the existing default; we don't reject
    partial updates because the form may only submit a subset."""
    base = dict(db.DEFAULT_NOTIFICATION_PREFS)
    mode = str(p.get("mode", base["mode"]))
    if mode not in _VALID_MODES:
        mode = base["mode"]
    base["mode"] = mode
    base["digest_hour"] = max(0, min(23, int(p.get("digest_hour", base["digest_hour"]))))
    base["threshold"] = max(1, min(99, int(p.get("threshold", base["threshold"]))))
    base["quiet_hours_enabled"] = bool(p.get("quiet_hours_enabled", base["quiet_hours_enabled"]))
    base["quiet_start_hour"] = max(
        0, min(23, int(p.get("quiet_start_hour", base["quiet_start_hour"])))
    )
    base["quiet_end_hour"] = max(0, min(23, int(p.get("quiet_end_hour", base["quiet_end_hour"]))))
    tz = str(p.get("tz", base["tz"]) or base["tz"])[:64]
    base["tz"] = tz
    return base


@app.get("/notify", response_class=HTMLResponse)
def notify_settings(request: Request, user: dict = Depends(current_user)):
    uid = user["tailscale_login"]
    prefs = db.get_notification_prefs(uid)
    devices = len(db.list_push_subscriptions(uid))
    return templates.TemplateResponse(
        "notify_settings.html",
        {
            "request": request,
            "user": user,
            "prefs": prefs,
            "devices": devices,
            "vapid_key": notify.public_key_b64(),
        },
    )


@app.post("/notify/prefs")
async def notify_prefs_save(request: Request, user: dict = Depends(current_user)):
    raw = await request.json()
    # Merge submitted values over the user's existing prefs so state-only
    # fields (last_digest_date, last_when_ready_at) survive an update.
    existing = db.get_notification_prefs(user["tailscale_login"])
    validated = _validate_prefs({**existing, **raw})
    # Preserve scheduler-managed state untouched.
    for k in ("last_digest_date", "last_when_ready_at"):
        validated[k] = existing.get(k)
    db.set_notification_prefs(user["tailscale_login"], validated)
    return JSONResponse({"ok": True, "prefs": validated})


@app.get("/notify/vapid-public-key")
def vapid_public_key():
    return JSONResponse({"key": notify.public_key_b64()})


@app.post("/notify/subscribe")
async def notify_subscribe(request: Request, user: dict = Depends(current_user)):
    sub = await request.json()
    if not isinstance(sub, dict) or "endpoint" not in sub:
        raise HTTPException(400, "bad subscription payload")
    notify.subscribe(user["tailscale_login"], sub)
    return JSONResponse({"ok": True})


@app.post("/notify/unsubscribe")
async def notify_unsubscribe(request: Request, user: dict = Depends(current_user)):
    """Remove a single device's push subscription. Endpoint is the natural
    key — same endpoint can only belong to one user, so the auth check
    here is for sanity (the endpoint is opaque and unguessable, so even
    without ownership filtering, an attacker would need the endpoint URL
    to remove someone else's subscription)."""
    body = await request.json()
    endpoint = body.get("endpoint") if isinstance(body, dict) else None
    if not endpoint:
        raise HTTPException(400, "missing endpoint")
    db.delete_push_subscription(endpoint)
    return JSONResponse({"ok": True})


@app.post("/notify/test")
async def notify_send_test(user: dict = Depends(current_user)):
    """Send a one-off "test push" to the current user's devices so they
    can verify subscription is alive end-to-end. Useful after first
    install or after toggling off→on."""
    res = notify.send_to_user(
        user["tailscale_login"],
        "Prep — test push",
        "If you can read this, notifications are working on this device.",
        url="/notify",
    )
    return JSONResponse(res)


# Move the staging-experiment subscription file out of the way and start
# the scheduler. Existing subscribers from the test will need to re-enable
# from the settings page (one-time friction; cleaner than carrying state
# in two stores).
@app.on_event("startup")
async def _notify_boot():
    legacy = BASE_DIR / "push-subscriptions.json"
    if legacy.exists():
        legacy.rename(legacy.with_suffix(".json.archived-pre-v0.5"))
    notify.start_scheduler()
