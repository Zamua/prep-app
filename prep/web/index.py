"""Index / home page route.

Cross-cuts the decks and study contexts (lists user's decks alongside
their recent study sessions), so it lives at the prep/web/ level
rather than under either context's routes module.

Also hosts the unauthenticated `/healthz` liveness probe used by the
container healthcheck + `docker compose up --wait`. It deliberately
does NOT touch the database — a slow / contended sqlite read should
not look like the app is down. If we later want a readiness probe
that exercises dependencies (db, agent, temporal), add `/readyz`
alongside.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from prep.auth.identity import optional_current_user
from prep.auth.providers import get_provider
from prep.decks.repo import DeckRepo
from prep.study.repo import SessionRepo
from prep.trivia.repo import TriviaQueueRepo, TriviaSessionsRepo
from prep.trivia.session_state import format_done
from prep.web.templates import templates

router = APIRouter()


def build_deck_lists_context(request: Request, uid: str) -> dict:
    """Build the subset of index-template context needed by
    partials/deck_lists.html. Reused by the pin route's htmx
    response so the in-place swap reflects fresh state without
    re-doing the full index render. Same data shape as the index
    handler builds — kept in lockstep here.
    """
    deck_repo = DeckRepo()
    session_repo = SessionRepo()
    trivia_repo = TriviaQueueRepo()
    summaries = deck_repo.list_summaries(uid)
    pinned: list[dict] = []
    others: list[dict] = []
    for d in summaries:
        item = d.model_dump()
        if d.deck_type == d.deck_type.TRIVIA:
            item["trivia_stats"] = trivia_repo.deck_stats(d.id)
        (pinned if d.pinned else others).append(item)
    recents = session_repo.list_recent(uid, limit=5)
    trivia_sessions = TriviaSessionsRepo()
    active_trivia = trivia_sessions.list_active(uid)
    snoozed_srs = session_repo.list_snoozed(uid)
    snoozed_trivia = trivia_sessions.list_snoozed(uid)
    is_new_user = not (
        pinned or others or recents or active_trivia or snoozed_srs or snoozed_trivia
    )
    return {
        "request": request,
        "pinned_decks": pinned,
        "decks": others,
        "is_new_user": is_new_user,
        "recent_sessions": [r.model_dump() for r in recents],
        "active_trivia_sessions": active_trivia,
    }


@router.get("/healthz", include_in_schema=False)
def healthz() -> PlainTextResponse:
    """Liveness probe. 200 = the uvicorn process is up and the route
    table loaded; that's intentionally all it asserts. No DB hit, no
    agent ping — those would be readiness, not liveness."""
    return PlainTextResponse("ok")


@router.get("/debug/session", response_class=HTMLResponse, include_in_schema=False)
def debug_session(request: Request) -> HTMLResponse:
    """Un-gated session-state readout for diagnosing the PWA
    'always prompts to sign in' bug. Shows side by side what the SERVER
    saw on THIS request (did it resolve a user? which Clerk cookies
    arrived?) and what CLIENT-side ClerkJS hydrates (Clerk.user, the
    cookies in document.cookie, standalone-PWA flag). Cookie NAMES and
    booleans only — no values, no secrets, only the requester's own
    state. Removable once the PWA session bug is closed."""
    import json
    import os

    from prep.web.templates import _clerk_bootstrap_context

    user = optional_current_user(request)
    cookie_hdr = request.headers.get("cookie", "")
    names = sorted({c.split("=", 1)[0].strip() for c in cookie_hdr.split(";") if "=" in c})
    uid = user.get("tailscale_login") if user else None
    ctx = _clerk_bootstrap_context(request)
    pk = (ctx.get("clerk_publishable_key") or "").strip()
    host = (ctx.get("clerk_frontend_api_host") or "").strip()
    server = {
        "server_resolved_user": user is not None,
        "server_uid_suffix": (uid[-6:] if uid else None),
        "auth_mode": (os.environ.get("PREP_AUTH_MODE") or "tailscale").strip().lower(),
        "cookie___session_present": "__session" in names,
        "cookie___client_uat_present": "__client_uat" in names,
        "all_cookie_names": names,
        "user_agent": request.headers.get("user-agent", ""),
    }
    server_json = json.dumps(server, indent=2)
    clerk_src = (
        f"https://{host}/npm/@clerk/clerk-js@5/dist/clerk.browser.js" if (pk and host) else ""
    )
    clerk_tag = (
        f'<script async crossorigin="anonymous" data-clerk-publishable-key="{pk}" '
        f'src="{clerk_src}"></script>'
        if clerk_src
        else "<!-- clerk not configured -->"
    )
    html_doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>prep session debug</title>"
        "<style>body{font:14px/1.5 ui-monospace,Menlo,monospace;margin:1rem;"
        "background:#111;color:#eee}h2{font-size:13px;color:#9cf;margin:1rem 0 .25rem}"
        "pre{background:#000;padding:.75rem;border-radius:6px;overflow:auto;"
        "white-space:pre-wrap;word-break:break-word}.k{color:#6c6}</style>"
        f"{clerk_tag}</head><body>"
        "<h1 style='font-size:15px'>prep · /debug/session</h1>"
        "<p>Open this in the PWA right after a cold launch (while it shows "
        "signed-out). Compare SERVER vs CLIENT below.</p>"
        "<h2 class='k'>SERVER saw (this request)</h2>"
        f"<pre>{server_json}</pre>"
        "<h2 class='k'>CLIENT (ClerkJS after load)</h2>"
        "<pre id='client'>loading ClerkJS…</pre>"
        "<script>"
        "(async function(){"
        "function cookieHas(n){return document.cookie.split(';').some(function(c){"
        "return c.trim().indexOf(n+'=')===0});}"
        "var out=document.getElementById('client');"
        "var waited=0;while(!window.Clerk&&waited<6000){"
        "await new Promise(function(r){setTimeout(r,50)});waited+=50;}"
        "var loaded=false,err=null;"
        "if(window.Clerk){try{await window.Clerk.load();loaded=true;}"
        "catch(e){err=String(e);}}"
        "var c=window.Clerk;"
        "var ss;try{ss=sessionStorage.getItem('clerk_reauth_reload');}catch(e){ss='n/a';}"
        "var data={"
        "standalone_pwa:(window.navigator.standalone===true)||"
        "window.matchMedia('(display-mode: standalone)').matches,"
        "clerkjs_present:!!window.Clerk,clerk_loaded:loaded,clerk_load_error:err,"
        "clerk_user_present:!!(c&&c.user),"
        "clerk_user_id_suffix:(c&&c.user)?String(c.user.id).slice(-6):null,"
        "clerk_session_present:!!(c&&c.session),"
        "cookie___session_present:cookieHas('__session'),"
        "cookie___client_uat_present:cookieHas('__client_uat'),"
        "sessionStorage_clerk_reauth_reload:ss"
        "};"
        "out.textContent=JSON.stringify(data,null,2);"
        "})();"
        "</script></body></html>"
    )
    return HTMLResponse(html_doc)


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    deck_repo: DeckRepo = Depends(DeckRepo),
    session_repo: SessionRepo = Depends(SessionRepo),
):
    """Home page.

    Unauthenticated visitors get the marketing landing page (sells
    the product, points at sign-in). Authenticated users get the
    dashboard — their decks plus the last five active study sessions
    across all decks (pinned-first ordering split into two groups).

    Branching on auth state at the SAME URL means link-sharing works
    naturally — `prepcards.app` is the canonical entrypoint whether
    you're a first-time visitor or a returning user."""
    user = optional_current_user(request)
    if user is None:
        urls = get_provider().urls()
        # ClerkJS itself is loaded by base.html on every page (see
        # _clerk_bootstrap_context in prep.web.templates). The landing
        # template uses `clerk_publishable_key` (also context-processor
        # supplied) only to gate its one-shot post-signup reload.
        return templates.TemplateResponse(
            "landing.html",
            {
                "request": request,
                "user": None,
                "sign_in_url": urls.sign_in,
            },
        )
    uid = user["tailscale_login"]
    summaries = deck_repo.list_summaries(uid)
    recents = session_repo.list_recent(uid, limit=5)
    # Trivia decks need extra stats for the mini mastery bar — total /
    # mastered / wrong / unanswered. SRS decks use the existing due/total
    # rendering and don't need this. One query per trivia deck is fine
    # at this scale (a single user has tens of decks at most).
    trivia_repo = TriviaQueueRepo()
    pinned: list[dict] = []
    others: list[dict] = []
    for d in summaries:
        item = d.model_dump()
        if d.deck_type == d.deck_type.TRIVIA:
            item["trivia_stats"] = trivia_repo.deck_stats(d.id)
        (pinned if d.pinned else others).append(item)
    # Active trivia sessions across all decks — powers the "Continue"
    # strip at the top of the home page so the user can resume any
    # in-progress session without going to the deck page first.
    trivia_sessions = TriviaSessionsRepo()
    active_trivia = trivia_sessions.list_active(uid)
    active_trivia_views = [
        {
            "deck_name": s.deck_name,
            "deck_display": s.deck_display,
            "deck_id": s.deck_id,
            "remaining": s.remaining,
            "total": s.total,
            "last_active": s.last_active,
            "queue_param": ",".join(str(q) for q in s.queue),
            "done_param": format_done(s.done),
        }
        for s in active_trivia
    ]
    # Snoozed sessions across both SRS + trivia, merged into one list
    # so the "Snoozed" sub-section renders as a single group ordered
    # by wake time. Each row carries the form action it needs to POST
    # to when the user adjusts/wakes — the template uses `kind` to
    # pick which URL pattern (sid for SRS, deck_name for trivia).
    snoozed_srs = session_repo.list_snoozed(uid)
    snoozed_trivia = trivia_sessions.list_snoozed(uid)
    snoozed_views = [
        {
            "kind": "srs",
            "id": s.id,
            "deck_name": s.deck_name,
            "deck_display": s.deck_display,
            "snoozed_until": s.snoozed_until,
        }
        for s in snoozed_srs
    ] + [
        {
            "kind": "trivia",
            "deck_name": s.deck_name,
            "deck_display": s.deck_display,
            "snoozed_until": s.snoozed_until,
        }
        for s in snoozed_trivia
    ]
    # Soonest wakes first — same order on both sides of the merge.
    snoozed_views.sort(key=lambda r: r["snoozed_until"] or "")
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "user": user,
            "pinned_decks": pinned,
            "decks": others,
            "recent_sessions": [r.model_dump() for r in recents],
            "active_trivia_sessions": active_trivia_views,
            "snoozed_sessions": snoozed_views,
        },
    )
