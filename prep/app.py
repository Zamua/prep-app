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
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.exceptions import HTTPException as StarletteHTTPException

# Probed once at module import (cheap — file stat / one HTTP call max).
# Surfaced via _agent_context so templates can gate AI-driven UI.
from prep import agent as _agent_mod
from prep import (
    chat_handoff,
    db,
    icons,
    notify,
    temporal_client,
)
from prep.domain import grading

# REPO_ROOT (not BASE_DIR) — the package now lives one level below the
# repo root, but `templates/` and `static/` stay at the repo root so
# they aren't shipped into the python package's import surface.
REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_DIR = REPO_ROOT

_AGENT_AVAILABLE = _agent_mod.probe()


def _user_context(request: Request) -> dict:
    """Jinja context_processor: surface `user` to every template that gets a
    Request whose state was populated by current_user(). Lets routes /
    error handlers omit the explicit `"user": user` entry without losing the
    masthead chip + base.html `data-editor-mode` attribute."""
    return {"user": getattr(request.state, "user", None)}


def _agent_context(request: Request) -> dict:
    """Jinja context_processor: every template gets `agent_available`.
    True when an AI agent (PREP_AGENT_URL or PREP_AGENT_BIN) is reachable
    at boot. Templates use it to hide AI-driven controls (Generate cards,
    Transform, Improve) and surface manual paths instead — so the app
    stays useful as a manual-flashcard SRS for users without claude
    installed."""
    return {"agent_available": _AGENT_AVAILABLE}


templates = Jinja2Templates(
    directory=str(BASE_DIR / "templates"),
    context_processors=[_user_context, _agent_context],
)

# Markdown rendering for prompts (and other free-form fields). Mistune escapes
# raw HTML by default — input is already trusted (we generated it ourselves)
# but we still want **bold** / `code` / fenced blocks / lists / headings to
# render rather than show as raw markdown text.
_md = mistune.create_markdown(
    escape=True,  # escape any raw HTML; we don't want pass-through
    hard_wrap=False,
    plugins=["strikethrough", "table"],
)


def _handoff_ctx(
    *,
    deck_name: str,
    q: dict,
    user_answer: str,
    verdict: dict | None,
    idk: bool,
    picked_set: list[str],
    correct_set: list[str],
) -> dict:
    """Build the AI-chat-handoff payload that result.html embeds as data
    attributes. Same shape across all three result-rendering paths
    (study_submit, session_view, grading_view terminal)."""
    msg = chat_handoff.build_message(
        deck_name=deck_name,
        q=q,
        user_answer=user_answer,
        verdict=verdict,
        idk=idk,
        picked_set=picked_set,
        correct_set=correct_set,
    )
    return {
        "handoff_message": msg,
        "handoff_urls": chat_handoff.provider_urls(msg),
        "handoff_default_provider": chat_handoff.DEFAULT_PROVIDER,
        "handoff_providers": chat_handoff.CHAT_PROVIDERS,
    }


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
from prep.web.responses import redirect as _redirect  # noqa: E402,I001 — placement matters


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
if _AGENT_AVAILABLE:
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

app.include_router(decks_router)

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

import re

# Deck names go in the URL, so they're constrained to URL-safe chars.
# Lowercase + digits + hyphens; must start alphanumeric; 2-30 chars.
_DECK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,29}$")
_RESERVED_DECK_NAMES = {
    "new",
    "create",
    "edit",
    "delete",
    "static",
    "dev",
    "preview",
    "notify",
    "session",
    "study",
    "deck",
    "decks",
    "manifest",
}
_MAX_CONTEXT_PROMPT_CHARS = 8000


def _validate_deck_name(name: str) -> str:
    n = (name or "").strip().lower()
    if not _DECK_NAME_RE.match(n):
        raise HTTPException(
            400,
            "Deck name must be 2-30 chars, lowercase, alphanumerics or hyphens, starting with a letter or digit.",
        )
    if n in _RESERVED_DECK_NAMES:
        raise HTTPException(400, f'"{n}" is reserved — pick another name.')
    return n


@app.get("/decks/new", response_class=HTMLResponse)
def deck_new_form(request: Request, user: dict = Depends(current_user)):
    return templates.TemplateResponse(
        "deck_new.html",
        {
            "request": request,
            "user": user,
            "name_value": "",
            "context_value": "",
            "error": None,
        },
    )


@app.post("/decks/new", response_class=HTMLResponse)
async def deck_new_create(request: Request, user: dict = Depends(current_user)):
    """Create a deck. The submit button name picks the path:
      action=empty       → just create the deck row, redirect to /deck/<name>
      action=plan        → create the deck row, kick off PlanGenerateWorkflow
                           with the description, redirect to /plan/<wid>
    The 'plan' action requires both an agent AND a non-empty description.
    Re-renders the form with an inline error if either is missing.
    """
    uid = user["tailscale_login"]
    form = await request.form()
    name = (form.get("name") or "").strip()
    context_prompt = (form.get("context_prompt") or "").strip()
    action = (form.get("action") or "empty").strip()

    def rerender(error: str, status: int = 400):
        return templates.TemplateResponse(
            "deck_new.html",
            {
                "request": request,
                "user": user,
                "name_value": name,
                "context_value": context_prompt,
                "error": error,
            },
            status_code=status,
        )

    try:
        clean = _validate_deck_name(name)
    except HTTPException as e:
        return rerender(e.detail)

    if db.find_deck(uid, clean) is not None:
        return rerender(f'You already have a deck named "{clean}".')

    if len(context_prompt) > _MAX_CONTEXT_PROMPT_CHARS:
        return rerender(
            f"Description is too long ({len(context_prompt)} chars; max {_MAX_CONTEXT_PROMPT_CHARS})."
        )

    if action == "plan":
        if not _AGENT_AVAILABLE:
            return rerender(
                "Plan & generate needs an AI agent. Set PREP_AGENT_URL or PREP_AGENT_BIN, or pick 'Create empty deck' instead."
            )
        if not context_prompt:
            return rerender("Plan & generate needs a description for claude to plan against.")

    deck_id = db.create_deck(uid, clean, context_prompt=context_prompt or None)

    if action == "plan":
        try:
            res = await temporal_client.start_plan_generate(
                user_id=uid,
                deck_id=deck_id,
                deck_name=clean,
                prompt=context_prompt,
            )
        except Exception as e:
            # Deck row was created but the workflow couldn't start. Don't
            # silently leave it empty — surface the error and the user can
            # retry from the deck page.
            raise HTTPException(500, f"deck created but failed to start plan workflow: {e}")
        return _redirect(request, f"/plan/{res.workflow_id}")

    return _redirect(request, f"/deck/{clean}")


@app.get("/deck/{name}", response_class=HTMLResponse)
def deck_view(request: Request, name: str, user: dict = Depends(current_user)):
    uid = user["tailscale_login"]
    deck_id = db.get_or_create_deck(uid, name)
    questions = db.list_questions(uid, deck_id)
    return templates.TemplateResponse(
        "deck.html",
        {
            "request": request,
            "user": user,
            "deck_name": name,
            "questions": questions,
            "due_count": sum(
                1
                for q in questions
                if not q["suspended"] and q["next_due"] and q["next_due"] <= db.now()
            ),
        },
    )


# ---- Study sessions (cross-device, version-checked) ------------------------


@app.post("/study/{name}/begin")
def session_begin(request: Request, name: str, fresh: int = 0, user: dict = Depends(current_user)):
    """Auto-resume an active session on this deck, or create a fresh one.
    Pass ?fresh=1 to abandon any existing active session and start over."""
    uid = user["tailscale_login"]
    deck_id = db.get_or_create_deck(uid, name)
    if not fresh:
        existing = db.find_active_session_for_deck(uid, deck_id)
        if existing:
            return _redirect(request, f"/session/{existing['id']}")
    # Mark prior abandoned (if fresh=1).
    if fresh:
        existing = db.find_active_session_for_deck(uid, deck_id)
        if existing:
            db.abandon_session(uid, existing["id"])
    label = db.device_label_from_ua(request.headers.get("user-agent"))
    sid = db.create_session(uid, deck_id, label)
    return _redirect(request, f"/session/{sid}")


@app.get("/session/{sid}", response_class=HTMLResponse)
def session_view(request: Request, sid: str, user: dict = Depends(current_user)):
    uid = user["tailscale_login"]
    s = db.get_session(uid, sid)
    if not s:
        raise HTTPException(404, "session not found")
    with db.cursor() as c:
        deck_name = c.execute(
            "SELECT name FROM decks WHERE id = ? AND user_id = ?",
            (s["deck_id"], uid),
        ).fetchone()["name"]

    # Branch by state.
    if s["status"] == "completed":
        return templates.TemplateResponse(
            "session_completed.html",
            {"request": request, "session": s, "deck_name": deck_name},
        )
    if s["status"] == "abandoned":
        # Treat as 404-ish for UX — let the caller go back to deck.
        return _redirect(request, f"/deck/{deck_name}")

    if s["state"] == "showing-result":
        # Render result.html using cached verdict + state from the session
        # row, plus the user_answer from the most recent reviews row for
        # this question (single source of truth, no extra column needed).
        qid = s["last_answered_qid"]
        q = db.get_question(uid, qid)
        verdict = s["last_answered_verdict"]
        st = s["last_answered_state"]
        with db.cursor() as c:
            r = c.execute(
                "SELECT user_answer FROM reviews WHERE question_id = ? " "ORDER BY id DESC LIMIT 1",
                (qid,),
            ).fetchone()
            user_answer = r["user_answer"] if r else ""
        idk = user_answer == ""  # idk submissions store empty user_answer
        # Mcq/multi need parsed picked + correct sets for the answer grid.
        picked_set: list[str] = []
        correct_set: list[str] = []
        if q and q["type"] in ("mcq", "multi"):
            try:
                if q["type"] == "multi":
                    picked_set = json.loads(user_answer) if user_answer else []
                    correct_set = json.loads(q["answer"]) if q.get("answer") else []
                else:  # mcq
                    picked_set = [user_answer] if user_answer else []
                    correct_set = [q["answer"]] if q.get("answer") else []
            except (json.JSONDecodeError, TypeError):
                picked_set, correct_set = [], []
        return templates.TemplateResponse(
            "result.html",
            {
                "request": request,
                "deck_name": deck_name,
                "q": q,
                "user_answer": user_answer,
                "idk": idk,
                "verdict": verdict,
                "state": st,
                "picked_set": picked_set,
                "correct_set": correct_set,
                "session_id": sid,
                "session_version": s["version"],
                **_handoff_ctx(
                    deck_name=deck_name,
                    q=q,
                    user_answer=user_answer,
                    verdict=verdict,
                    idk=idk,
                    picked_set=picked_set,
                    correct_set=correct_set,
                ),
            },
        )

    if s["state"] == "grading":
        # Reuse the existing grading polling page; on completion reconcile.
        return _redirect(request, f"/grading/{s['current_grading_workflow_id']}")

    # awaiting-answer: render a session-aware study card.
    q = db.get_question(uid, s["current_question_id"]) if s["current_question_id"] else None
    if not q:
        # No more due cards — flip to completed.
        with db.cursor() as c:
            c.execute(
                "UPDATE study_sessions SET status='completed', "
                "       version = version + 1, last_active = ? "
                " WHERE id = ? AND user_id = ?",
                (db.now(), sid, uid),
            )
        return _redirect(request, f"/session/{sid}")
    return templates.TemplateResponse(
        "session.html",
        {
            "request": request,
            "user": user,
            "session": s,
            "deck_name": deck_name,
            "q": q,
            "draft": s.get("current_draft") or (q.get("skeleton") or ""),
        },
    )


@app.post("/session/{sid}/draft")
async def session_draft(request: Request, sid: str, user: dict = Depends(current_user)):
    """Autosave endpoint. Body: {version: int, draft: str}. Returns
    {version: new} or 409 with {current_version: int}."""
    body = await request.json()
    try:
        new_v = db.update_session_draft(
            user["tailscale_login"],
            sid,
            body.get("draft", ""),
            int(body["version"]),
        )
    except db.StaleVersionError as e:
        return JSONResponse(
            {"error": "stale", "current_version": e.current_version},
            status_code=409,
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    return JSONResponse({"version": new_v})


@app.post("/session/{sid}/submit", response_class=HTMLResponse)
async def session_submit(request: Request, sid: str, user: dict = Depends(current_user)):
    form = await request.form()
    qtype = form["type"]
    qid = int(form["question_id"])
    expected_version = int(form["version"])
    idk = form.get("idk") == "1"
    uid = user["tailscale_login"]

    s = db.get_session(uid, sid)
    if not s:
        raise HTTPException(404, "session not found")

    if idk:
        user_answer = ""
    elif qtype == "mcq":
        user_answer = form.get("choice", "")
    elif qtype == "multi":
        user_answer = json.dumps(sorted(form.getlist("choice")))
    else:
        user_answer = form.get("answer", "")

    question = db.get_question(uid, qid)
    if not question:
        raise HTTPException(404, "question not found")

    # ---- Slow path: code/short go through the Temporal workflow when
    # an agent is available. No-agent mode renders a self-grade form
    # (sync, no workflow) so the session keeps advancing manually.
    if qtype in ("code", "short") and not idk:
        with db.cursor() as c:
            deck_name = c.execute(
                "SELECT name FROM decks WHERE id = ? AND user_id = ?",
                (s["deck_id"], uid),
            ).fetchone()["name"]

        if not _AGENT_AVAILABLE:
            return templates.TemplateResponse(
                "self_grade.html",
                {
                    "request": request,
                    "deck_name": deck_name,
                    "q": question,
                    "user_answer": user_answer,
                    "session_id": sid,
                    "session_version": expected_version,
                },
            )

        try:
            res = await temporal_client.start_grading(
                qid,
                deck_name,
                user_answer,
                idk,
                user_id=uid,
            )
            db.set_session_grading(uid, sid, qid, res.workflow_id, expected_version)
        except db.StaleVersionError as e:
            return _stale_response(request, sid, e.current_version)
        except Exception as e:
            raise HTTPException(500, f"failed to start grading workflow: {e}")
        # Sid carried as query param so the polling page can reconcile back
        # into the session on completion.
        return _redirect(request, f"/grading/{res.workflow_id}?sid={sid}")

    # ---- Fast path: idk + mcq/multi grade synchronously.
    verdict = grading.grade(question, user_answer, idk=idk)
    state = db.record_review(
        uid, qid, verdict["result"], user_answer, notes=verdict.get("feedback", "")
    )
    try:
        db.record_session_answer_sync(
            uid,
            sid,
            qid,
            expected_version,
            user_answer,
            verdict,
            state,
        )
    except db.StaleVersionError as e:
        return _stale_response(request, sid, e.current_version)
    return _redirect(request, f"/session/{sid}")


@app.post("/session/{sid}/advance")
async def session_advance(request: Request, sid: str, user: dict = Depends(current_user)):
    form = await request.form()
    expected_version = int(form["version"])
    try:
        db.advance_session(user["tailscale_login"], sid, expected_version)
    except db.StaleVersionError as e:
        return _stale_response(request, sid, e.current_version)
    return _redirect(request, f"/session/{sid}")


@app.post("/session/{sid}/abandon")
def session_abandon(request: Request, sid: str, user: dict = Depends(current_user)):
    uid = user["tailscale_login"]
    db.abandon_session(uid, sid)
    s = db.get_session(uid, sid)
    deck_name = ""
    if s:
        with db.cursor() as c:
            row = c.execute(
                "SELECT name FROM decks WHERE id = ? AND user_id = ?",
                (s["deck_id"], uid),
            ).fetchone()
            if row:
                deck_name = row["name"]
    return _redirect(request, f"/deck/{deck_name}" if deck_name else "/")


def _stale_response(request: Request, sid: str, current_version: int):
    """A submit/advance that arrives with a stale version — render a small
    'session moved' page that a tap reloads from server."""
    return templates.TemplateResponse(
        "session_stale.html",
        {
            "request": request,
            "session_id": sid,
            "current_version": current_version,
        },
        status_code=409,
    )


@app.get("/study/{name}", response_class=HTMLResponse)
def study(request: Request, name: str, user: dict = Depends(current_user)):
    uid = user["tailscale_login"]
    deck_id = db.get_or_create_deck(uid, name)
    due = db.due_questions(uid, deck_id, limit=1)
    if not due:
        return templates.TemplateResponse(
            "study_empty.html",
            {"request": request, "deck_name": name},
        )
    return templates.TemplateResponse(
        "study.html",
        {"request": request, "user": user, "deck_name": name, "q": due[0]},
    )


@app.post("/study/{name}", response_class=HTMLResponse)
async def study_submit(request: Request, name: str, user: dict = Depends(current_user)):
    form = await request.form()
    qid = int(form["question_id"])
    qtype = form["type"]
    idk = form.get("idk") == "1"
    uid = user["tailscale_login"]

    if idk:
        user_answer = ""
    elif qtype == "mcq":
        user_answer = form.get("choice", "")
    elif qtype == "multi":
        user_answer = json.dumps(sorted(form.getlist("choice")))
    else:
        user_answer = form.get("answer", "")

    question = db.get_question(uid, qid)
    if not question:
        raise HTTPException(404, "question not found")

    # ---- Slow path: code/short go through the GradeAnswerWorkflow so the
    # browser doesn't hang for 10-30s on the claude -p shell-out. The worker
    # grades + records via Temporal activities; we 303 to a polling page.
    #
    # No-agent mode: skip the workflow and render a self-grade form. The
    # user compares their answer to the canonical and picks right/wrong;
    # a small sync POST records the review. Same outcome as AI grading,
    # different judge.
    if qtype in ("code", "short") and not idk:
        if not _AGENT_AVAILABLE:
            return templates.TemplateResponse(
                "self_grade.html",
                {
                    "request": request,
                    "deck_name": name,
                    "q": question,
                    "user_answer": user_answer,
                    "session_id": None,
                    "session_version": None,
                },
            )
        try:
            res = await temporal_client.start_grading(
                qid,
                name,
                user_answer,
                idk,
                user_id=uid,
            )
        except Exception as e:
            raise HTTPException(500, f"failed to start grading workflow: {e}")
        return _redirect(request, f"/grading/{res.workflow_id}")

    # ---- Fast path: idk + mcq/multi grade synchronously (deterministic, ms).
    verdict = grading.grade(question, user_answer, idk=idk)
    state = db.record_review(
        uid,
        qid,
        verdict["result"],
        user_answer,
        notes=verdict.get("feedback", ""),
    )

    # Pre-parse user/correct answers for the template so it can render type-
    # appropriate UI without doing JSON-decoding inside Jinja.
    picked_set: list[str] = []
    correct_set: list[str] = []
    if qtype in ("mcq", "multi"):
        try:
            if qtype == "multi":
                picked_set = json.loads(user_answer) if user_answer else []
                correct_set = json.loads(question["answer"]) if question.get("answer") else []
            else:  # mcq
                picked_set = [user_answer] if user_answer else []
                correct_set = [question["answer"]] if question.get("answer") else []
        except (json.JSONDecodeError, TypeError):
            picked_set, correct_set = [], []

    return templates.TemplateResponse(
        "result.html",
        {
            "request": request,
            "deck_name": name,
            "q": question,
            "user_answer": user_answer,
            "idk": idk,
            "verdict": verdict,
            "state": state,
            # For mcq/multi: parsed sets; template uses them to colour each choice.
            "picked_set": picked_set,
            "correct_set": correct_set,
            **_handoff_ctx(
                deck_name=name,
                q=question,
                user_answer=user_answer,
                verdict=verdict,
                idk=idk,
                picked_set=picked_set,
                correct_set=correct_set,
            ),
        },
    )


# ---- Grading workflow polling page + result render -------------------------


def _parse_grading_wid(wid: str) -> tuple[str, int] | None:
    """workflow IDs are formatted `grade-<deck>-q<qid>-<rand>`.
    Returns (deck_name, question_id) or None if the format doesn't match."""
    if not wid.startswith("grade-"):
        return None
    parts = wid[len("grade-") :].split("-")
    # Find the q-prefixed segment (deck name itself can contain hyphens, e.g.
    # "temporal-prep" if we add one). Walk from the right: rand is last,
    # q<qid> is second-to-last, everything before is the deck name.
    if len(parts) < 3:
        return None
    qid_part = parts[-2]
    if not qid_part.startswith("q"):
        return None
    try:
        qid = int(qid_part[1:])
    except ValueError:
        return None
    deck_name = "-".join(parts[:-2])
    return deck_name, qid


@app.get("/grading/{wid}", response_class=HTMLResponse)
async def grading_view(
    request: Request, wid: str, sid: str = "", user: dict = Depends(current_user)
):
    parsed = _parse_grading_wid(wid)
    if not parsed:
        raise HTTPException(400, "malformed workflow id")
    deck_name, qid = parsed
    uid = user["tailscale_login"]

    # Ownership gate — must verify before exposing ANY workflow state, not
    # just at terminal time. Otherwise user A can poll user B's in-progress
    # grading by guessing the wid (deck/qid are in the wid; the verdict
    # itself is gated below at terminal). This upfront check makes the
    # polling phase consistent with terminal: 404 on mismatch.
    if db.get_question(uid, qid) is None:
        raise HTTPException(404, "question not found")

    progress = await temporal_client.get_grade_progress(wid)
    desc = await temporal_client.describe_workflow(wid)
    status = (progress or {}).get("status") or (desc or {}).get("status") or "unknown"

    # Once the workflow is done, render the SAME result.html the synchronous
    # path uses — pulling verdict + state from the workflow result and the
    # question from the DB. Keeps the post-grade UI consistent across paths.
    terminal = status in {"done", "failed", "COMPLETED", "FAILED", "CANCELED", "TERMINATED"}
    if terminal:
        result = await temporal_client.get_grade_result(wid)
        if result is None:
            return templates.TemplateResponse(
                "grading.html",
                {
                    "request": request,
                    "wid": wid,
                    "deck_name": deck_name,
                    "progress": progress,
                    "desc": desc,
                    "failed": True,
                },
            )
        question = db.get_question(uid, qid)
        if not question:
            raise HTTPException(404, "question not found")

        # Build the same template context the synchronous path provides.
        verdict = result["verdict"]
        state_raw = result["state"]
        # Map field names: SRS state from Go uses interval_minutes already.
        state = {
            "step": state_raw["step"],
            "next_due": state_raw["next_due"],
            "interval_minutes": state_raw["interval_minutes"],
        }

        # Session reconciliation: if this grade was started from a session,
        # the session has been sitting in state='grading' waiting for us to
        # transition it to 'showing-result'. session_grading_completed is
        # idempotent — safe to call on every render.
        if sid:
            db.session_grading_completed(uid, sid, qid, verdict, state, wid)
            # Once reconciled, the canonical view is /session/{sid} (state
            # showing-result). Redirect there so subsequent loads land on
            # the session URL, not the grading URL.
            return _redirect(request, f"/session/{sid}")

        picked_set: list[str] = []
        correct_set: list[str] = []
        # code/short never have choices — leave the sets empty for the template.
        return templates.TemplateResponse(
            "result.html",
            {
                "request": request,
                "deck_name": deck_name,
                "q": question,
                "user_answer": result["user_answer"],
                "idk": result["idk"],
                "verdict": verdict,
                "state": state,
                "picked_set": picked_set,
                "correct_set": correct_set,
                **_handoff_ctx(
                    deck_name=deck_name,
                    q=question,
                    user_answer=result["user_answer"],
                    verdict=verdict,
                    idk=result["idk"],
                    picked_set=picked_set,
                    correct_set=correct_set,
                ),
            },
        )

    # Still grading — render the polling page.
    return templates.TemplateResponse(
        "grading.html",
        {
            "request": request,
            "wid": wid,
            "deck_name": deck_name,
            "progress": progress,
            "desc": desc,
            "failed": False,
            "sid": sid,
        },
    )


@app.get("/grading/{wid}/status")
async def grading_status(wid: str, user: dict = Depends(current_user)):
    parsed = _parse_grading_wid(wid)
    if not parsed:
        raise HTTPException(400, "malformed workflow id")
    _, qid = parsed
    if db.get_question(user["tailscale_login"], qid) is None:
        raise HTTPException(404, "question not found")
    progress = await temporal_client.get_grade_progress(wid)
    desc = await temporal_client.describe_workflow(wid)
    return JSONResponse({"progress": progress, "desc": desc})


@app.post("/study/{name}/self-grade/{qid}")
async def study_self_grade(
    request: Request, name: str, qid: int, user: dict = Depends(current_user)
):
    """No-agent grading. The user submitted a code/short answer, the
    workflow path was skipped (no agent to grade with), and they picked
    right/wrong themselves on the self_grade.html page. We record a
    normal review and either advance the session or bounce back to the
    deck index."""
    uid = user["tailscale_login"]
    q = db.get_question(uid, qid)
    if not q:
        raise HTTPException(404, "question not found")

    form = await request.form()
    verdict_str = form.get("verdict", "")
    if verdict_str not in ("right", "wrong"):
        raise HTTPException(422, "verdict must be 'right' or 'wrong'")
    user_answer = form.get("user_answer", "")
    sid = form.get("session_id") or None
    sver = form.get("session_version") or None

    verdict = {"result": verdict_str, "feedback": "(self-graded)"}
    state = db.record_review(uid, qid, verdict_str, user_answer, notes="(self-graded)")

    if sid and sver:
        try:
            db.record_session_answer_sync(
                uid,
                sid,
                qid,
                int(sver),
                user_answer,
                verdict,
                state,
            )
        except db.StaleVersionError as e:
            return _stale_response(request, sid, e.current_version)
        return _redirect(request, f"/session/{sid}")
    return _redirect(request, f"/deck/{name}")


@app.get("/deck/{name}/question/new", response_class=HTMLResponse)
def question_new(request: Request, name: str, user: dict = Depends(current_user)):
    """Manual question entry form. Becomes the primary card-creation
    path when no AI agent is configured; an additional path otherwise."""
    uid = user["tailscale_login"]
    if not db.find_deck(uid, name):
        raise HTTPException(404, "deck not found")
    return templates.TemplateResponse(
        "question_new.html",
        {"request": request, "deck_name": name, "form": {}, "error": None},
    )


@app.post("/deck/{name}/question/new", response_class=HTMLResponse)
async def question_new_submit(request: Request, name: str, user: dict = Depends(current_user)):
    uid = user["tailscale_login"]
    deck_id = db.find_deck(uid, name)
    if deck_id is None:
        raise HTTPException(404, "deck not found")

    form = await request.form()
    qtype = (form.get("type") or "").strip()
    prompt = (form.get("prompt") or "").strip()
    answer_raw = (form.get("answer") or "").strip()
    topic = (form.get("topic") or "").strip() or None
    skeleton = (form.get("skeleton") or "").strip() or None
    language = (form.get("language") or "").strip() or None
    rubric = (form.get("rubric") or "").strip() or None
    # Choices are entered one per line.
    choices_raw = (form.get("choices") or "").strip()
    choices = [ln.strip() for ln in choices_raw.splitlines() if ln.strip()] or None

    err = None
    if qtype not in db.QUESTION_TYPES:
        err = f"Type must be one of: {', '.join(sorted(db.QUESTION_TYPES))}."
    elif not prompt:
        err = "Prompt is required."
    elif not answer_raw:
        err = "Answer is required."
    elif qtype in ("mcq", "multi") and not choices:
        err = f"{qtype.upper()} questions need at least one choice (one per line)."
    elif qtype == "code" and not language:
        err = "Code questions need a language."

    if err:
        return templates.TemplateResponse(
            "question_new.html",
            {
                "request": request,
                "deck_name": name,
                "form": {
                    "type": qtype,
                    "prompt": prompt,
                    "answer": answer_raw,
                    "topic": topic or "",
                    "skeleton": skeleton or "",
                    "language": language or "",
                    "rubric": rubric or "",
                    "choices": choices_raw,
                },
                "error": err,
            },
            status_code=400,
        )

    answer: object = answer_raw
    if qtype == "multi":
        # Stored as JSON array. Accept either a JSON literal or a
        # newline-separated list (same as choices) — be forgiving.
        try:
            parsed = json.loads(answer_raw)
            if isinstance(parsed, list):
                answer = parsed
        except json.JSONDecodeError:
            answer = [ln.strip() for ln in answer_raw.splitlines() if ln.strip()]

    db.add_question(
        uid,
        deck_id,
        qtype,
        prompt,
        answer,
        topic=topic,
        choices=choices,
        rubric=rubric,
        skeleton=skeleton,
        language=language,
    )
    return _redirect(request, f"/deck/{name}")


def _question_form_from_row(q: dict) -> dict:
    """Convert a db.get_question row into the dict shape the
    question_edit/question_new template's `form` block expects:
    list-typed fields rendered as newline-joined strings, multi-answer
    JSON unwrapped, etc."""
    answer = q.get("answer") or ""
    if q.get("type") == "multi":
        try:
            parsed = json.loads(answer) if answer else []
            if isinstance(parsed, list):
                answer = "\n".join(parsed)
        except (json.JSONDecodeError, TypeError):
            pass  # leave as-is
    choices_text = ""
    if q.get("choices_list"):
        choices_text = "\n".join(q["choices_list"])
    return {
        "type": q.get("type") or "",
        "topic": q.get("topic") or "",
        "prompt": q.get("prompt") or "",
        "choices": choices_text,
        "answer": answer,
        "rubric": q.get("rubric") or "",
        "skeleton": q.get("skeleton") or "",
        "language": q.get("language") or "",
    }


def _deck_name_for_question(uid: str, deck_id: int) -> str | None:
    with db.cursor() as c:
        row = c.execute(
            "SELECT name FROM decks WHERE id = ? AND user_id = ?",
            (deck_id, uid),
        ).fetchone()
    return row["name"] if row else None


@app.get("/question/{qid}/edit", response_class=HTMLResponse)
def question_edit_form(request: Request, qid: int, user: dict = Depends(current_user)):
    """Manual edit form. Always available regardless of agent — this is
    the manual counterpart to the AI Improve button."""
    uid = user["tailscale_login"]
    q = db.get_question(uid, qid)
    if not q:
        raise HTTPException(404, "question not found")
    deck_name = _deck_name_for_question(uid, q["deck_id"])
    if not deck_name:
        raise HTTPException(404, "deck not found")
    return templates.TemplateResponse(
        "question_edit.html",
        {
            "request": request,
            "deck_name": deck_name,
            "q": q,
            "form": _question_form_from_row(q),
            "error": None,
        },
    )


@app.post("/question/{qid}/edit", response_class=HTMLResponse)
async def question_edit_submit(request: Request, qid: int, user: dict = Depends(current_user)):
    uid = user["tailscale_login"]
    q = db.get_question(uid, qid)
    if not q:
        raise HTTPException(404, "question not found")
    deck_name = _deck_name_for_question(uid, q["deck_id"])
    if not deck_name:
        raise HTTPException(404, "deck not found")

    form = await request.form()
    qtype = (form.get("type") or "").strip()
    prompt = (form.get("prompt") or "").strip()
    answer_raw = (form.get("answer") or "").strip()
    topic = (form.get("topic") or "").strip() or None
    skeleton = (form.get("skeleton") or "").strip() or None
    language = (form.get("language") or "").strip() or None
    rubric = (form.get("rubric") or "").strip() or None
    choices_raw = (form.get("choices") or "").strip()
    choices = [ln.strip() for ln in choices_raw.splitlines() if ln.strip()] or None

    err = None
    if qtype not in db.QUESTION_TYPES:
        err = f"Type must be one of: {', '.join(sorted(db.QUESTION_TYPES))}."
    elif not prompt:
        err = "Prompt is required."
    elif not answer_raw:
        err = "Answer is required."
    elif qtype in ("mcq", "multi") and not choices:
        err = f"{qtype.upper()} questions need at least one choice (one per line)."
    elif qtype == "code" and not language:
        err = "Code questions need a language."

    if err:
        return templates.TemplateResponse(
            "question_edit.html",
            {
                "request": request,
                "deck_name": deck_name,
                "q": q,
                "form": {
                    "type": qtype,
                    "prompt": prompt,
                    "answer": answer_raw,
                    "topic": topic or "",
                    "skeleton": skeleton or "",
                    "language": language or "",
                    "rubric": rubric or "",
                    "choices": choices_raw,
                },
                "error": err,
            },
            status_code=400,
        )

    answer: object = answer_raw
    if qtype == "multi":
        try:
            parsed = json.loads(answer_raw)
            if isinstance(parsed, list):
                answer = parsed
        except json.JSONDecodeError:
            answer = [ln.strip() for ln in answer_raw.splitlines() if ln.strip()]

    db.update_question(
        uid,
        qid,
        qtype=qtype,
        prompt=prompt,
        answer=answer,
        topic=topic,
        choices=choices,
        rubric=rubric,
        skeleton=skeleton,
        language=language,
    )
    return _redirect(request, f"/deck/{deck_name}")


# /question/{qid}/suspend + /unsuspend moved to prep.decks.routes.


# ---- Transform (card-level improve + deck-level prompt) -------------------
#
# Replaces the count-based "+ N cards" generate flow with a free-text prompt
# claude interprets. Two scopes:
#   • POST /question/{qid}/improve — auto-applies the rewrite
#   • POST /deck/{name}/transform  — returns a Plan, redirects to preview;
#                                    user signals apply or reject


def _parse_transform_wid(wid: str) -> tuple[str, int] | None:
    """transform workflow IDs are `transform-<scope>-<target_id>-<rand>`.
    Returns (scope, target_id) or None if malformed. scope ∈ {card, deck}."""
    if not wid.startswith("transform-"):
        return None
    parts = wid[len("transform-") :].split("-")
    if len(parts) < 3:
        return None
    scope = parts[0]
    if scope not in ("card", "deck"):
        return None
    try:
        target_id = int(parts[1])
    except ValueError:
        return None
    return scope, target_id


def _require_owns_transform(user: dict, wid: str) -> tuple[str, int]:
    parsed = _parse_transform_wid(wid)
    if not parsed:
        raise HTTPException(400, "malformed workflow id")
    scope, target_id = parsed
    uid = user["tailscale_login"]
    if scope == "card":
        if db.get_question(uid, target_id) is None:
            raise HTTPException(404, "transform not found")
    else:
        with db.cursor() as c:
            row = c.execute(
                "SELECT name FROM decks WHERE id = ? AND user_id = ?",
                (target_id, uid),
            ).fetchone()
        if not row:
            raise HTTPException(404, "transform not found")
    return scope, target_id


# /question/{qid}/improve moved to prep.decks.routes.


# /deck/{name}/delete moved to prep.decks.routes (mounted below).


@app.post("/deck/{name}/transform")
async def deck_transform(
    request: Request, name: str, prompt: str = Form(...), user: dict = Depends(current_user)
):
    """Deck-level free-text transform — replaces the old generate-N flow.
    Returns a Plan and waits on apply/reject signal before writing."""
    uid = user["tailscale_login"]
    deck_id = db.get_or_create_deck(uid, name)  # materialize if first time
    if not prompt.strip():
        raise HTTPException(400, "empty prompt")
    try:
        result = await temporal_client.start_transform(
            user_id=uid,
            scope="deck",
            target_id=deck_id,
            prompt=prompt.strip(),
        )
    except Exception as e:
        raise HTTPException(500, f"failed to start transform: {e}")
    return _redirect(request, f"/transform/{result.workflow_id}")


@app.get("/transform/{wid}", response_class=HTMLResponse)
async def transform_view(request: Request, wid: str, user: dict = Depends(current_user)):
    scope, target_id = _require_owns_transform(user, wid)
    progress = await temporal_client.get_transform_progress(wid)
    desc = await temporal_client.describe_workflow(wid)
    status = (progress or {}).get("status") or (desc or {}).get("status") or "unknown"

    # On terminal completion the workflow's queryable handler is gone —
    # fall back to the awaited result. Combine into one shape the template
    # can render uniformly.
    terminal = status in {
        "done",
        "failed",
        "rejected",
        "COMPLETED",
        "FAILED",
        "TERMINATED",
        "CANCELED",
    }
    if terminal and progress is None:
        progress = {"status": "done", "result": await temporal_client.get_transform_result(wid)}

    deck_name = ""
    if scope == "deck":
        with db.cursor() as c:
            row = c.execute(
                "SELECT name FROM decks WHERE id = ? AND user_id = ?",
                (target_id, user["tailscale_login"]),
            ).fetchone()
        if row:
            deck_name = row["name"]
    else:
        # card scope — find the deck name via the question for the back link
        q = db.get_question(user["tailscale_login"], target_id)
        if q:
            with db.cursor() as c:
                row = c.execute(
                    "SELECT name FROM decks WHERE id = ? AND user_id = ?",
                    (q["deck_id"], user["tailscale_login"]),
                ).fetchone()
            if row:
                deck_name = row["name"]

    return templates.TemplateResponse(
        "transform.html",
        {
            "request": request,
            "user": user,
            "wid": wid,
            "scope": scope,
            "target_id": target_id,
            "deck_name": deck_name,
            "progress": progress or {},
            "desc": desc or {},
            "status": status,
        },
    )


@app.get("/transform/{wid}/status")
async def transform_status(wid: str, user: dict = Depends(current_user)):
    _require_owns_transform(user, wid)
    progress = await temporal_client.get_transform_progress(wid)
    desc = await temporal_client.describe_workflow(wid)
    return JSONResponse({"progress": progress, "desc": desc})


@app.post("/transform/{wid}/apply")
async def transform_apply(request: Request, wid: str, user: dict = Depends(current_user)):
    _require_owns_transform(user, wid)
    try:
        await temporal_client.signal_apply_transform(wid)
    except Exception as e:
        raise HTTPException(500, f"signal failed: {e}")
    return _redirect(request, f"/transform/{wid}")


@app.post("/transform/{wid}/reject")
async def transform_reject(request: Request, wid: str, user: dict = Depends(current_user)):
    _require_owns_transform(user, wid)
    try:
        await temporal_client.signal_reject_transform(wid)
    except Exception as e:
        raise HTTPException(500, f"signal failed: {e}")
    return _redirect(request, f"/transform/{wid}")


# ---- Plan-first generation polling page + signals -------------------------
#
# Workflow IDs are formatted `plan-<deck_name>-<10-hex>`. Deck names may
# contain hyphens, so we split off the trailing rand suffix to recover the
# name. Ownership is verified by looking up the deck on the current user.


def _parse_plan_wid(wid: str) -> str | None:
    """Returns the deck_name embedded in a plan wid, or None if malformed."""
    if not wid.startswith("plan-"):
        return None
    rest = wid[len("plan-") :]
    if "-" not in rest:
        return None
    name, _, suffix = rest.rpartition("-")
    if not name or len(suffix) < 6:
        return None
    return name


def _require_owns_plan(user: dict, wid: str) -> tuple[str, int]:
    name = _parse_plan_wid(wid)
    if not name:
        raise HTTPException(400, "malformed workflow id")
    uid = user["tailscale_login"]
    deck_id = db.find_deck(uid, name)  # returns int|None, not a row
    if deck_id is None:
        raise HTTPException(404, "plan not found")
    return name, deck_id


@app.get("/plan/{wid}", response_class=HTMLResponse)
async def plan_view(request: Request, wid: str, user: dict = Depends(current_user)):
    deck_name, _ = _require_owns_plan(user, wid)
    progress = await temporal_client.get_plan_progress(wid)
    if progress is None:
        # Workflow gone — query handler is unavailable. The deck page is
        # the canonical place to land.
        return _redirect(request, f"/deck/{deck_name}")
    return templates.TemplateResponse(
        "plan.html",
        {
            "request": request,
            "wid": wid,
            "deck_name": deck_name,
            "progress": progress,
        },
    )


@app.get("/plan/{wid}/status")
async def plan_status(wid: str, user: dict = Depends(current_user)):
    """JSON status for the polling page. Returns the live PlanGenerateProgress
    while the workflow is alive, or {"status": "gone"} once the query
    handler is no longer registered."""
    _require_owns_plan(user, wid)
    progress = await temporal_client.get_plan_progress(wid)
    if progress is None:
        return JSONResponse({"status": "gone"})
    return JSONResponse(progress)


@app.post("/plan/{wid}/feedback")
async def plan_feedback(request: Request, wid: str, user: dict = Depends(current_user)):
    _require_owns_plan(user, wid)
    form = await request.form()
    fb = (form.get("feedback") or "").strip()
    if not fb:
        raise HTTPException(400, "feedback is required")
    try:
        await temporal_client.signal_plan_feedback(wid, fb)
    except Exception as e:
        raise HTTPException(500, f"signal failed: {e}")
    return _redirect(request, f"/plan/{wid}")


@app.post("/plan/{wid}/accept")
async def plan_accept(request: Request, wid: str, user: dict = Depends(current_user)):
    _require_owns_plan(user, wid)
    try:
        await temporal_client.signal_plan_accept(wid)
    except Exception as e:
        raise HTTPException(500, f"signal failed: {e}")
    return _redirect(request, f"/plan/{wid}")


@app.post("/plan/{wid}/reject")
async def plan_reject(request: Request, wid: str, user: dict = Depends(current_user)):
    deck_name, _ = _require_owns_plan(user, wid)
    try:
        await temporal_client.signal_plan_reject(wid)
    except Exception as e:
        raise HTTPException(500, f"signal failed: {e}")
    # Reject = abandon. Bounce back to the (still-empty) deck.
    return _redirect(request, f"/deck/{deck_name}")


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
    """Re-probe the agent and update the global flag the context_processor
    surfaces. Called after a connect/disconnect so the UI sees the new
    state immediately."""
    global _AGENT_AVAILABLE
    s = _agent_mod.status()
    _AGENT_AVAILABLE = bool(s.get("logged_in"))
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
