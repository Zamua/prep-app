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

import base64
import os

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


def _clerk_frontend_api_host() -> str | None:
    """Decode Clerk publishable key to find the Frontend API host.

    `pk_<env>_<base64>` where base64 is `<host>$` — e.g.
    `pk_live_Y2xlcmsucHJlcGNhcmRzLmFwcCQ` → `clerk.prepcards.app`.
    Returns None when no key is set (Tailscale-mode or misconfig)."""
    pk = (os.environ.get("CLERK_PUBLISHABLE_KEY") or "").strip()
    if not pk or "_" not in pk:
        return None
    encoded = pk.split("_", 2)[-1]
    try:
        # Clerk pads with trailing `$` instead of `=`; tolerate either.
        padded = encoded + "=" * (-len(encoded) % 4)
        decoded = base64.b64decode(padded).decode("ascii", errors="ignore")
    except Exception:  # noqa: BLE001
        return None
    return decoded.rstrip("$").strip() or None


@router.get("/healthz", include_in_schema=False)
def healthz() -> PlainTextResponse:
    """Liveness probe. 200 = the uvicorn process is up and the route
    table loaded; that's intentionally all it asserts. No DB hit, no
    agent ping — those would be readiness, not liveness."""
    return PlainTextResponse("ok")


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
        return templates.TemplateResponse(
            "landing.html",
            {
                "request": request,
                "user": None,
                "sign_in_url": urls.sign_in,
                # ClerkJS bootstrap config — when set, the landing
                # page loads Clerk's browser SDK, which exchanges the
                # `__client_uat` apex cookie for a `__session` JWT
                # cookie and reloads so the server sees the session.
                # Without this step the user signs in, lands back
                # here, and stays stuck on the landing page because
                # only ClerkJS can mint the apex __session cookie.
                "clerk_publishable_key": (os.environ.get("CLERK_PUBLISHABLE_KEY") or "").strip()
                or None,
                "clerk_frontend_api_host": _clerk_frontend_api_host(),
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
        {"kind": "srs", "id": s.id, "deck_name": s.deck_name, "snoozed_until": s.snoozed_until}
        for s in snoozed_srs
    ] + [
        {"kind": "trivia", "deck_name": s.deck_name, "snoozed_until": s.snoozed_until}
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
