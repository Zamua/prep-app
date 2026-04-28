"""FastAPI app for the interview-prep flashcard tool.

Routes:
  GET  /                                -> deck list
  GET  /deck/{name}                     -> deck overview (questions + Add More + Study)
  POST /deck/{name}/add                 -> kick off a Temporal workflow that generates N cards;
                                           redirects to the generation status page
  GET  /generation/{wid}                -> live progress page (polls the workflow's getProgress query)
  GET  /generation/{wid}/status         -> JSON progress (consumed by the polling JS)
  POST /generation/{wid}/cancel         -> send the cancelGeneration signal
  GET  /study/{name}                    -> next due card
  POST /study/{name}                    -> submit answer, grade, advance SRS
  POST /question/{id}/suspend / unsuspend
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
import mistune
from starlette.exceptions import HTTPException as StarletteHTTPException

import chat_handoff
import db
import generator
import grader
import icons
import notify
import temporal_client

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Markdown rendering for prompts (and other free-form fields). Mistune escapes
# raw HTML by default — input is already trusted (we generated it ourselves)
# but we still want **bold** / `code` / fenced blocks / lists / headings to
# render rather than show as raw markdown text.
_md = mistune.create_markdown(
    escape=True,           # escape any raw HTML; we don't want pass-through
    hard_wrap=False,
    plugins=["strikethrough", "table"],
)


def _handoff_ctx(*, deck_name: str, q: dict, user_answer: str, verdict: dict | None,
                 idk: bool, picked_set: list[str], correct_set: list[str]) -> dict:
    """Build the AI-chat-handoff payload that result.html embeds as data
    attributes. Same shape across all three result-rendering paths
    (study_submit, session_view, grading_view terminal)."""
    msg = chat_handoff.build_message(
        deck_name=deck_name, q=q, user_answer=user_answer,
        verdict=verdict, idk=idk,
        picked_set=picked_set, correct_set=correct_set,
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
    401: ("Not signed in.", "Open prep on a tailnet device — your Tailscale identity is what authenticates you."),
    403: ("Forbidden.", "That's not yours to look at."),
    404: ("Not found.", "We couldn't find what you were looking for. Maybe a typo, or the link is stale."),
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
    if path.endswith("/subscribe") or path.endswith("/unsubscribe") \
       or path.endswith("/test") or path.endswith("/prefs") \
       or path.endswith("/vapid-public-key"):
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
    import logging, traceback
    logging.getLogger("prep").error(
        "unhandled exception on %s %s: %s\n%s",
        request.method, request.url.path, exc, traceback.format_exc(),
    )
    if _wants_json(request):
        return JSONResponse({"detail": "internal server error"}, status_code=500)
    return _render_error(request, 500)


def _redirect(request: Request, path: str, status_code: int = 303) -> RedirectResponse:
    """Build a RedirectResponse whose Location header includes the request's
    root_path. FastAPI's RedirectResponse takes the URL verbatim — it does
    NOT auto-prepend root_path — so a bare /deck/foo would land outside the
    /prep/ Caddy route and the user gets a white screen. Hit on 2026-04-26.
    """
    prefix = request.scope.get("root_path", "") or ""
    if path.startswith("/"):
        return RedirectResponse(f"{prefix}{path}", status_code=status_code)
    return RedirectResponse(f"{prefix}/{path}", status_code=status_code)


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


def _resolve_login(request: Request) -> str | None:
    hdr = request.headers.get("tailscale-user-login")
    if hdr:
        return hdr.strip()
    fallback = os.environ.get("PREP_DEFAULT_USER")
    return fallback or None


def current_user(request: Request) -> dict:
    login = _resolve_login(request)
    if not login:
        raise HTTPException(401, "no Tailscale identity (set Tailscale-User-Login header or PREP_DEFAULT_USER)")
    display_name = request.headers.get("tailscale-user-name") or login.split("@", 1)[0]
    profile_pic = request.headers.get("tailscale-user-profile-pic") or None
    return db.upsert_user(login, display_name, profile_pic)


db.init()
# No boot-seed: deck rows materialize per-user the first time they navigate
# to /deck/{name} (or hit any route that calls get_or_create_deck). The
# index page enumerates DECK_CONTEXT directly so configured decks appear
# even before they exist in DB. Avoids the previous design where seeding
# under PREP_DEFAULT_USER (or its "owner@local" fallback) leaked dev
# fixtures into the prod users/decks tables on every restart.

# Dev-only template preview routes for the UI sweep — read-only, no DB writes.
import dev_preview
dev_preview.register(app, templates)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, user: dict = Depends(current_user)):
    uid = user["tailscale_login"]
    # Merge DECK_CONTEXT (the configured catalog) with DB rows for this user.
    # Configured decks the user hasn't materialized yet show as 0/0; they get
    # a real row in `decks` the first time the user navigates to /deck/{name}.
    db_decks = {d["name"]: d for d in db.list_decks(uid)}
    decks: list[dict] = []
    for name in generator.DECK_CONTEXT:
        decks.append(db_decks.pop(name, {"name": name, "total": 0, "due": 0}))
    # Append any decks the user has that aren't in DECK_CONTEXT (legacy or
    # decks added then removed from the catalog) so they aren't orphaned.
    decks.extend(db_decks.values())
    decks.sort(key=lambda d: d["name"])
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
_RESERVED_DECK_NAMES = {"new", "create", "edit", "delete", "static",
                        "dev", "preview", "notify", "session", "study",
                        "deck", "decks", "manifest"}
_MAX_CONTEXT_PROMPT_CHARS = 8000


def _validate_deck_name(name: str) -> str:
    n = (name or "").strip().lower()
    if not _DECK_NAME_RE.match(n):
        raise HTTPException(400, "Deck name must be 2-30 chars, lowercase, alphanumerics or hyphens, starting with a letter or digit.")
    if n in _RESERVED_DECK_NAMES:
        raise HTTPException(400, f"\"{n}\" is reserved — pick another name.")
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
def deck_new_create(request: Request,
                    name: str = Form(...),
                    context_prompt: str = Form(""),
                    user: dict = Depends(current_user)):
    uid = user["tailscale_login"]
    try:
        clean = _validate_deck_name(name)
    except HTTPException as e:
        # Re-render the form with the error inline so the user keeps their
        # context_prompt. Form errors don't deserve a full error page.
        return templates.TemplateResponse(
            "deck_new.html",
            {"request": request, "user": user,
             "name_value": name, "context_value": context_prompt,
             "error": e.detail},
            status_code=400,
        )
    if db.find_deck(uid, clean) is not None:
        return templates.TemplateResponse(
            "deck_new.html",
            {"request": request, "user": user,
             "name_value": name, "context_value": context_prompt,
             "error": f"You already have a deck named \"{clean}\"."},
            status_code=400,
        )
    cp = (context_prompt or "").strip()
    if len(cp) > _MAX_CONTEXT_PROMPT_CHARS:
        return templates.TemplateResponse(
            "deck_new.html",
            {"request": request, "user": user,
             "name_value": name, "context_value": context_prompt,
             "error": f"Description is too long ({len(cp)} chars; max {_MAX_CONTEXT_PROMPT_CHARS})."},
            status_code=400,
        )
    db.create_deck(uid, clean, context_prompt=cp or None)
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
            "due_count": sum(1 for q in questions
                              if not q["suspended"] and q["next_due"] and q["next_due"] <= db.now()),
        },
    )


@app.post("/deck/{name}/add")
async def deck_add(request: Request, name: str, count: int = Form(5),
                    user: dict = Depends(current_user)):
    # The deck must exist in DB. UI-created decks have a context_prompt;
    # legacy bootstrap decks (cherry, temporal) fall back to DECK_CONTEXT
    # in the worker's loadDeckContext.
    uid = user["tailscale_login"]
    if db.find_deck(uid, name) is None and name not in generator.DECK_CONTEXT:
        raise HTTPException(404, f"Unknown deck \"{name}\".")
    count = max(1, min(count, 15))
    try:
        result = await temporal_client.start_generation(
            name, count, user_id=user["tailscale_login"],
        )
    except Exception as e:
        raise HTTPException(500, f"failed to start workflow: {e}")
    return _redirect(request, f"/generation/{result.workflow_id}")


def _parse_generation_wid(wid: str) -> str | None:
    """Workflow IDs are formatted `gen-<deck>-<rand>`. Returns deck_name
    or None if malformed. Deck names can contain hyphens, so we walk from
    the right (rand is always the last segment)."""
    if not wid.startswith("gen-"):
        return None
    parts = wid[len("gen-"):].split("-")
    if len(parts) < 2:
        return None
    return "-".join(parts[:-1])


def _require_owns_generation(user: dict, wid: str) -> str:
    """Verifies the current user owns the deck this workflow is generating
    cards for. Returns deck_name on success, raises HTTPException otherwise.
    Used as the auth gate for /generation/{wid}* routes — without this, any
    authed user could poll/cancel any other user's generation by guessing
    the workflow id."""
    deck_name = _parse_generation_wid(wid)
    if not deck_name:
        raise HTTPException(400, "malformed workflow id")
    deck_id = db.find_deck(user["tailscale_login"], deck_name)
    if deck_id is None:
        raise HTTPException(404, "workflow not found")
    return deck_name


@app.get("/generation/{wid}", response_class=HTMLResponse)
async def generation_view(request: Request, wid: str,
                          user: dict = Depends(current_user)):
    deck_name = _require_owns_generation(user, wid)
    progress = await temporal_client.get_progress(wid)
    desc = await temporal_client.describe_workflow(wid)
    return templates.TemplateResponse(
        "generation.html",
        {
            "request": request,
            "wid": wid,
            "deck_name": deck_name,
            "progress": progress,
            "desc": desc,
        },
    )


@app.get("/generation/{wid}/status")
async def generation_status(wid: str, user: dict = Depends(current_user)):
    _require_owns_generation(user, wid)
    progress = await temporal_client.get_progress(wid)
    desc = await temporal_client.describe_workflow(wid)
    return JSONResponse({"progress": progress, "desc": desc})


@app.post("/generation/{wid}/cancel")
async def generation_cancel(request: Request, wid: str,
                            user: dict = Depends(current_user)):
    _require_owns_generation(user, wid)
    try:
        await temporal_client.cancel_generation(wid)
    except Exception as e:
        raise HTTPException(500, f"cancel failed: {e}")
    return _redirect(request, f"/generation/{wid}")


# ---- Study sessions (cross-device, version-checked) ------------------------


@app.post("/study/{name}/begin")
def session_begin(request: Request, name: str, fresh: int = 0,
                   user: dict = Depends(current_user)):
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
                "SELECT user_answer FROM reviews WHERE question_id = ? "
                "ORDER BY id DESC LIMIT 1", (qid,)
            ).fetchone()
            user_answer = r["user_answer"] if r else ""
        idk = (user_answer == "")  # idk submissions store empty user_answer
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
                **_handoff_ctx(deck_name=deck_name, q=q, user_answer=user_answer,
                               verdict=verdict, idk=idk,
                               picked_set=picked_set, correct_set=correct_set),
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
            user["tailscale_login"], sid,
            body.get("draft", ""), int(body["version"]),
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

    # ---- Slow path: code/short go through the Temporal workflow.
    if qtype in ("code", "short") and not idk:
        # Look up deck name for the workflow id format.
        with db.cursor() as c:
            deck_name = c.execute(
                "SELECT name FROM decks WHERE id = ? AND user_id = ?",
                (s["deck_id"], uid),
            ).fetchone()["name"]
        try:
            res = await temporal_client.start_grading(
                qid, deck_name, user_answer, idk, user_id=uid,
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
    verdict = grader.grade(question, user_answer, idk=idk)
    state = db.record_review(uid, qid, verdict["result"], user_answer,
                              notes=verdict.get("feedback", ""))
    try:
        db.record_session_answer_sync(
            uid, sid, qid, expected_version, user_answer, verdict, state,
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
        {"request": request, "deck_name": name, "q": due[0]},
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
    if qtype in ("code", "short") and not idk:
        try:
            res = await temporal_client.start_grading(
                qid, name, user_answer, idk, user_id=uid,
            )
        except Exception as e:
            raise HTTPException(500, f"failed to start grading workflow: {e}")
        return _redirect(request, f"/grading/{res.workflow_id}")

    # ---- Fast path: idk + mcq/multi grade synchronously (deterministic, ms).
    verdict = grader.grade(question, user_answer, idk=idk)
    state = db.record_review(
        uid, qid, verdict["result"], user_answer,
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
            **_handoff_ctx(deck_name=name, q=question, user_answer=user_answer,
                           verdict=verdict, idk=idk,
                           picked_set=picked_set, correct_set=correct_set),
        },
    )


# ---- Grading workflow polling page + result render -------------------------


def _parse_grading_wid(wid: str) -> tuple[str, int] | None:
    """workflow IDs are formatted `grade-<deck>-q<qid>-<rand>`.
    Returns (deck_name, question_id) or None if the format doesn't match."""
    if not wid.startswith("grade-"):
        return None
    parts = wid[len("grade-"):].split("-")
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
async def grading_view(request: Request, wid: str, sid: str = "",
                         user: dict = Depends(current_user)):
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
                {"request": request, "wid": wid, "deck_name": deck_name,
                 "progress": progress, "desc": desc, "failed": True},
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
                **_handoff_ctx(deck_name=deck_name, q=question,
                               user_answer=result["user_answer"],
                               verdict=verdict, idk=result["idk"],
                               picked_set=picked_set, correct_set=correct_set),
            },
        )

    # Still grading — render the polling page.
    return templates.TemplateResponse(
        "grading.html",
        {"request": request, "wid": wid, "deck_name": deck_name,
         "progress": progress, "desc": desc, "failed": False, "sid": sid},
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


@app.post("/question/{qid}/suspend")
def suspend(request: Request, qid: int, user: dict = Depends(current_user)):
    uid = user["tailscale_login"]
    q = db.get_question(uid, qid)
    if not q:
        raise HTTPException(404, "question not found")
    db.set_suspended(uid, qid, True)
    with db.cursor() as c:
        name = c.execute(
            "SELECT name FROM decks WHERE id=? AND user_id=?",
            (q["deck_id"], uid),
        ).fetchone()["name"]
    return _redirect(request, f"/deck/{name}")


@app.post("/question/{qid}/unsuspend")
def unsuspend(request: Request, qid: int, user: dict = Depends(current_user)):
    uid = user["tailscale_login"]
    q = db.get_question(uid, qid)
    if not q:
        raise HTTPException(404, "question not found")
    db.set_suspended(uid, qid, False)
    with db.cursor() as c:
        name = c.execute(
            "SELECT name FROM decks WHERE id=? AND user_id=?",
            (q["deck_id"], uid),
        ).fetchone()["name"]
    return _redirect(request, f"/deck/{name}")


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
    return JSONResponse({
        "name": f"prep · a commonplace book{' (staging)' if env_label else ''}",
        "short_name": short,
        "description": "Spaced-repetition flashcards for interview prep.",
        "display": "standalone",
        "scope": (root + "/") or "/",
        "start_url": (root + "/") or "/",
        "background_color": "#f4ecdc",
        "theme_color": "#f5efe6",
        "icons": [
            {"src": f"{root}/static/pwa/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": f"{root}/static/pwa/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    })


@app.get("/sw.js")
def service_worker():
    """Serve the SW from the app's root scope (rather than /static/sw.js
    whose default scope is /static/). The browser uses the SW's URL path
    as its scope, so this URL is what determines what the SW controls."""
    return FileResponse(BASE_DIR / "static" / "sw.js", media_type="application/javascript")


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
    base["quiet_start_hour"] = max(0, min(23, int(p.get("quiet_start_hour", base["quiet_start_hour"]))))
    base["quiet_end_hour"] = max(0, min(23, int(p.get("quiet_end_hour", base["quiet_end_hour"]))))
    tz = str(p.get("tz", base["tz"]) or base["tz"])[:64]
    base["tz"] = tz
    return base


@app.get("/notify", response_class=HTMLResponse)
def notify_settings(request: Request, user: dict = Depends(current_user)):
    uid = user["tailscale_login"]
    prefs = db.get_notification_prefs(uid)
    devices = len(db.list_push_subscriptions(uid))
    return templates.TemplateResponse("notify_settings.html", {
        "request": request,
        "user": user,
        "prefs": prefs,
        "devices": devices,
        "vapid_key": notify.public_key_b64(),
    })


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
