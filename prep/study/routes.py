"""HTML routes for the study bounded context.

Page-level entry points only: begin or resume a session, serve the
study shell for a session or a deck, and the session controls the
dashboard posts to (abandon, snooze). The loop itself (next card,
submit, grading, authoring) runs in the browser against
prep.study.api, so no route here renders a card, a verdict, or a
grading page.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from prep.auth import current_user
from prep.auth.providers import get_provider
from prep.decks.entities import DeckType
from prep.decks.repo import DeckRepo, QuestionRepo
from prep.study import service
from prep.study.entities import SessionStatus
from prep.study.repo import ReviewRepo, SessionRepo
from prep.web import responses
from prep.web.templates import templates

router = APIRouter()


# ---- per-request repo dependencies --------------------------------------


def _session_repo() -> SessionRepo:
    return SessionRepo()


def _review_repo() -> ReviewRepo:
    return ReviewRepo()


def _deck_repo() -> DeckRepo:
    return DeckRepo()


def _question_repo() -> QuestionRepo:
    return QuestionRepo()


# ---- session lifecycle --------------------------------------------------


@router.post("/study/{name}/begin")
def session_begin(
    request: Request,
    name: str,
    fresh: int = 0,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    session_repo: SessionRepo = Depends(_session_repo),
):
    """Auto-resume an active session on this deck, or create a fresh one.
    Pass ?fresh=1 to abandon any existing active session and start over."""
    uid = user["tailscale_login"]
    deck_id = deck_repo.get_or_create(uid, name)
    # Trivia decks are notification-driven — they have no SRS state
    # and the per-card answer flow lives in /trivia/*. Refuse the
    # study path so a stale bookmark doesn't create an empty SRS
    # session against a deck that has no `cards` rows.
    if deck_repo.get_type(uid, deck_id) is DeckType.TRIVIA:
        raise HTTPException(400, "trivia decks are notification-driven; no study sessions")
    if not fresh:
        existing = service.find_active_session(session_repo, uid, deck_id)
        if existing:
            return responses.redirect(request, f"/session/{existing.id}")
    if fresh:
        existing = service.find_active_session(session_repo, uid, deck_id)
        if existing:
            service.abandon_session(session_repo, uid, existing.id)
    label = session_repo.device_label_from_ua(request.headers.get("user-agent"))
    sid = service.start_session(session_repo, uid, deck_id, label)
    return responses.redirect(request, f"/session/{sid}")


@router.get("/session/{sid}", response_class=HTMLResponse)
def session_view(
    request: Request,
    sid: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    session_repo: SessionRepo = Depends(_session_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
    review_repo: ReviewRepo = Depends(_review_repo),
):
    """Render the study shell for this session. The shell's host reads
    the session's real state through the JSON study API, so every
    branch this route used to render (awaiting-answer, showing-result,
    grading, completed) is decided client-side from one source of
    truth. An abandoned session still redirects server-side: there is
    nothing for the loop to resume."""
    uid = user["tailscale_login"]
    s = service.get_session(session_repo, uid, sid)
    if s is None:
        raise HTTPException(404, "session not found")
    deck_name = deck_repo.find_name(uid, s.deck_id) or ""
    if s.status is SessionStatus.ABANDONED:
        return responses.redirect(request, f"/deck/{deck_name}")
    return templates.TemplateResponse(
        "study_shell.html",
        {
            "request": request,
            "user": user,
            "deck_name": deck_name,
            "session_id": sid,
            "sign_in_url": get_provider().urls().sign_in,
        },
    )


# ---- session mutations --------------------------------------------------


@router.post("/session/{sid}/abandon")
def session_abandon(
    request: Request,
    sid: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    session_repo: SessionRepo = Depends(_session_repo),
):
    """Manually kill a session. Returns the user to the deck page."""
    uid = user["tailscale_login"]
    s = service.get_session(session_repo, uid, sid)
    service.abandon_session(session_repo, uid, sid)
    deck_name = ""
    if s is not None:
        deck_name = deck_repo.find_name(uid, s.deck_id) or ""
    return responses.redirect(request, f"/deck/{deck_name}" if deck_name else "/")


@router.post("/session/{sid}/snooze")
async def session_snooze(
    request: Request,
    sid: str,
    user: dict = Depends(current_user),
    session_repo: SessionRepo = Depends(_session_repo),
):
    """Hide a session from the index Continue strip until a duration
    passes. Driven by the bottom-sheet picker in the session-card
    overflow menu; accepts either a `preset` (1h / tonight / tomorrow
    / 1d / 1w / wake / …) OR a `custom` integer + `unit`.

    Session row stays status='active' — list_recent filters by
    snoozed_until so it just doesn't surface until the timestamp is
    in the past. `preset=wake` clears the snooze (immediate wake) —
    used by the adjust sheet on already-snoozed sessions."""
    from prep.web.durations import DurationError, parse_until

    uid = user["tailscale_login"]
    form = await request.form()
    preset = (form.get("preset") or "").strip().lower()
    if preset == "wake":
        session_repo.snooze(uid, sid, None)
        return responses.redirect(request, "/")
    try:
        until = parse_until(
            preset=preset or None,
            custom=form.get("custom"),
            unit=form.get("unit"),
        )
    except DurationError as e:
        raise HTTPException(400, str(e)) from e
    session_repo.snooze(uid, sid, until)
    return responses.redirect(request, "/")


# ---- legacy no-session study path --------------------------------------


@router.get("/study/{name}", response_class=HTMLResponse)
def study(
    request: Request,
    name: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    review_repo: ReviewRepo = Depends(_review_repo),
):
    """Sessionless single-card study path. Renders the same shell the
    session path does, minus a session id: the host studies the deck
    directly through the JSON API. Most users land on
    /study/{name}/begin, which spins up a session and redirects."""
    uid = user["tailscale_login"]
    deck_repo.get_or_create(uid, name)
    return templates.TemplateResponse(
        "study_shell.html",
        {
            "request": request,
            "user": user,
            "deck_name": name,
            "session_id": None,
            "sign_in_url": get_provider().urls().sign_in,
        },
    )


def _parse_grading_wid(wid: str) -> tuple[str, int] | None:
    return service.parse_grading_wid(wid)


@router.get("/grading/{wid}", response_class=HTMLResponse)
async def grading_view(
    request: Request,
    wid: str,
    sid: str = "",
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
    session_repo: SessionRepo = Depends(_session_repo),
):
    """Preserved entry point for links minted while grading was its own
    polling page (notifications, bookmarks). The study shell owns the
    in-flight grade now, so send the browser there: the session resumes
    on whatever screen the grade actually reached.

    The workflow id names a deck and a question, so it is attacker
    supplied: both must belong to the caller. Without that check a
    crafted id would redirect into /study/{name}, whose get-or-create
    would mint decks in the visitor's account."""
    parsed = service.parse_grading_wid(wid)
    if not parsed:
        raise HTTPException(400, "malformed workflow id")
    deck_name, qid = parsed
    uid = user["tailscale_login"]
    if q_repo.get(uid, qid) is None:
        raise HTTPException(404, "no such grading job")
    if deck_repo.find_id(uid, deck_name) is None:
        raise HTTPException(404, "no such grading job")
    if sid and service.get_session(session_repo, uid, sid) is not None:
        return responses.redirect(request, f"/session/{sid}")
    return responses.redirect(request, f"/study/{deck_name}")
