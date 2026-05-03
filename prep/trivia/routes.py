"""HTTP routes for the trivia bounded context.

Two surfaces:

1. The mobile card view (`GET /trivia/<question_id>`) — minimal,
   single-card layout designed for one-tap-from-push usage. Question
   text + free-response textbox + submit. No "next card" UI; the
   user dismisses and goes back to whatever they were doing.

2. The answer endpoint (`POST /trivia/<question_id>/answer`) — grades
   via `prep.trivia.service.grade_answer` (deterministic
   normalization), persists the verdict + rotates the card to the
   back of its deck's queue via `TriviaQueueRepo.mark_answered`, and
   re-renders the same card with the result panel revealed (correct
   answer + dismiss button per the spec).

3. Manual generate (`POST /trivia/decks/<deck_id>/generate`) — admin
   trigger to call the agent + drop a fresh batch into the deck.
   Used by the deck-creation flow (initial batch) and as a one-off
   refill button on the deck page.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from prep.auth import current_user
from prep.decks.repo import DeckRepo, QuestionRepo
from prep.trivia import service as trivia_service
from prep.trivia.agent_client import AgentUnavailable
from prep.trivia.repo import TriviaQueueRepo
from prep.web import responses
from prep.web.templates import templates

router = APIRouter()


# ---- generation polling ------------------------------------------------


def _parse_trivia_wid(wid: str) -> str | None:
    """`trivia-<deck_name>-<rand>`. Returns deck_name or None on
    malformed input. The deck_name segment may itself contain hyphens,
    so we partition on the trailing rand suffix."""
    if not wid.startswith("trivia-"):
        return None
    rest = wid[len("trivia-") :]
    if "-" not in rest:
        return None
    name, _, suffix = rest.rpartition("-")
    if not name or len(suffix) < 6:
        return None
    return name


@router.get("/trivia/gen/{wid}", response_class=HTMLResponse)
def trivia_generating(wid: str, request: Request, user: dict = Depends(current_user)):
    """Polling page that watches the TriviaGenerateWorkflow. Shows
    progress (asking_claude → inserting → done), then redirects to
    the deck page when the workflow's `done`. The deck row was
    already created sync in /decks/new/trivia."""
    deck_name = _parse_trivia_wid(wid)
    if not deck_name:
        raise HTTPException(400, "malformed trivia workflow id")
    # IDOR guard — confirm the deck belongs to this user.
    decks = DeckRepo()
    if decks.find_id(user["tailscale_login"], deck_name) is None:
        raise HTTPException(404, "deck not found")
    return templates.TemplateResponse(
        request,
        "trivia/generating.html",
        {"wid": wid, "deck_name": deck_name},
    )


@router.get("/trivia/gen/{wid}/status")
async def trivia_generating_status(wid: str, user: dict = Depends(current_user)):
    """JSON poll endpoint. Returns the workflow's TriviaGenerateProgress.
    On `done`, surfaces the deck_name so the JS poller knows where to
    redirect."""
    from prep import temporal_client

    deck_name = _parse_trivia_wid(wid)
    if not deck_name:
        raise HTTPException(400, "malformed workflow id")
    if DeckRepo().find_id(user["tailscale_login"], deck_name) is None:
        raise HTTPException(404, "deck not found")
    progress = await temporal_client.get_trivia_progress(wid)
    if progress is None:
        # Workflow finished + dropped its query handler. Treat as done.
        progress = {"status": "done"}
    progress["deck_name"] = deck_name
    return JSONResponse(progress)


# ---- mini-session (notification target) -------------------------------
#
# Notifications deep-link to /trivia/session/<deck_name>. The session
# is stateless: a comma-separated `cards` query param holds the
# remaining queue. Each answer pops the head and redirects to the
# next, so refresh / back-button behavior stays sensible. When the
# list empties, the summary view renders.


def _parse_card_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    out: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            out.append(int(chunk))
    return out


@router.get("/trivia/session/{deck_name}", response_class=HTMLResponse)
def trivia_session(
    deck_name: str,
    request: Request,
    cards: str | None = None,
    user: dict = Depends(current_user),
):
    """Notification deep-link target: a 3-card mini-session. With no
    `cards` param, server picks a fresh session and redirects with the
    queue encoded. With an empty `cards`, renders the summary. Otherwise
    renders the head card; submit pops the head and redirects."""
    uid = user["tailscale_login"]
    decks = DeckRepo()
    questions = QuestionRepo()
    trivia = TriviaQueueRepo()
    deck_id = decks.find_id(uid, deck_name)
    if deck_id is None:
        raise HTTPException(404, "deck not found")

    if cards is None:
        # Fresh session — pick + redirect with cards encoded so the
        # remaining queue survives back-button + refresh.
        session = trivia.pick_session_for_deck(deck_id)
        ids = ",".join(str(c.question_id) for c in session)
        root = request.scope.get("root_path", "") or ""
        return HTMLResponse(
            f'<!doctype html><meta charset="utf-8">'
            f'<meta http-equiv="refresh" content="0; url={root}/trivia/session/{deck_name}?cards={ids}">',
            status_code=200,
        )

    queue = _parse_card_ids(cards)
    if not queue:
        return templates.TemplateResponse(
            request,
            "trivia/session_done.html",
            {"deck_name": deck_name},
        )

    head = queue[0]
    q = questions.get(uid, head)
    if q is None or q.deck_id != deck_id:
        # Skip cards the user can't access (stale URL after a deck
        # delete, or someone trying to inject foreign question_ids).
        remaining = ",".join(str(i) for i in queue[1:])
        root = request.scope.get("root_path", "") or ""
        return HTMLResponse(
            f'<!doctype html><meta charset="utf-8">'
            f'<meta http-equiv="refresh" content="0; url={root}/trivia/session/{deck_name}?cards={remaining}">',
            status_code=200,
        )
    return templates.TemplateResponse(
        request,
        "trivia/card.html",
        {
            "q": q,
            "deck_name": deck_name,
            "result": None,
            "session_position": 1,
            "session_total": len(queue),
            "session_remaining": cards,
        },
    )


@router.post("/trivia/session/{deck_name}/answer", response_class=HTMLResponse)
def trivia_session_answer(
    deck_name: str,
    request: Request,
    cards: str = Form(""),
    answer: str = Form(""),
    user: dict = Depends(current_user),
):
    """Grade the head card, mark_answered, redirect to GET with the
    head popped. If the head card was the last one, the redirect lands
    on the summary view."""
    uid = user["tailscale_login"]
    decks = DeckRepo()
    questions = QuestionRepo()
    trivia = TriviaQueueRepo()
    deck_id = decks.find_id(uid, deck_name)
    if deck_id is None:
        raise HTTPException(404, "deck not found")

    queue = _parse_card_ids(cards)
    if not queue:
        return responses.redirect(request, f"/trivia/session/{deck_name}?cards=")

    head = queue[0]
    q = questions.get(uid, head)
    if q is None or q.deck_id != deck_id:
        # Stale / foreign card — pop and continue.
        remaining = ",".join(str(i) for i in queue[1:])
        return responses.redirect(request, f"/trivia/session/{deck_name}?cards={remaining}")

    correct = trivia_service.grade_answer(expected=q.answer, given=answer)
    trivia.mark_answered(head, correct=correct)
    remaining = ",".join(str(i) for i in queue[1:])
    return templates.TemplateResponse(
        request,
        "trivia/card.html",
        {
            "q": q,
            "deck_name": deck_name,
            "result": {
                "correct": correct,
                "given": answer,
                "expected": q.answer,
            },
            "session_position": 1,
            "session_total": len(queue),
            "session_remaining": remaining,
        },
    )


@router.get("/trivia/{question_id}", response_class=HTMLResponse)
def trivia_card(
    question_id: int,
    request: Request,
    user: dict = Depends(current_user),
):
    """Render the minimal card view. Used as the deep-link target
    from the push notification body. Renders blank/no-result-yet
    until the user submits."""
    questions = QuestionRepo()
    decks = DeckRepo()
    q = questions.get(user["tailscale_login"], question_id)
    if q is None:
        raise HTTPException(404, "question not found")
    deck_name = decks.find_name(user["tailscale_login"], q.deck_id)
    return templates.TemplateResponse(
        request,
        "trivia/card.html",
        {
            "q": q,
            "deck_name": deck_name,
            "result": None,
        },
    )


@router.post("/trivia/{question_id}/answer", response_class=HTMLResponse)
def trivia_answer(
    question_id: int,
    request: Request,
    answer: str = Form(""),
    user: dict = Depends(current_user),
):
    """Grade + record + rotate. Re-renders the same card with the
    result block populated; user dismisses to leave."""
    questions = QuestionRepo()
    decks = DeckRepo()
    trivia = TriviaQueueRepo()

    q = questions.get(user["tailscale_login"], question_id)
    if q is None:
        raise HTTPException(404, "question not found")

    correct = trivia_service.grade_answer(expected=q.answer, given=answer)
    trivia.mark_answered(question_id, correct=correct)
    deck_name = decks.find_name(user["tailscale_login"], q.deck_id)
    return templates.TemplateResponse(
        request,
        "trivia/card.html",
        {
            "q": q,
            "deck_name": deck_name,
            "result": {
                "correct": correct,
                "given": answer,
                "expected": q.answer,
            },
        },
    )


@router.post("/trivia/decks/{deck_id}/notifications")
def trivia_toggle_notifications(
    deck_id: int,
    request: Request,
    enabled: str = Form(...),
    user: dict = Depends(current_user),
):
    """Flip the per-deck notification cycle on or off. Form posts
    `enabled=on` / `enabled=off`. 404 for non-trivia decks (the
    repo's `deck_type='trivia'` filter rejects them) so srs decks
    can't accidentally pick up a flag they don't honor."""
    decks = DeckRepo()
    if not decks.set_notifications_enabled(user["tailscale_login"], deck_id, enabled == "on"):
        raise HTTPException(404, "trivia deck not found")
    deck_name = decks.find_name(user["tailscale_login"], deck_id) or ""
    root = request.scope.get("root_path", "") or ""
    return HTMLResponse(
        f'<!doctype html><meta charset="utf-8">'
        f'<meta http-equiv="refresh" content="0; url={root}/deck/{deck_name}">'
        f"<p>Notifications {'enabled' if enabled == 'on' else 'disabled'}. "
        f'<a href="{root}/deck/{deck_name}">Back to deck</a>.</p>',
        status_code=200,
    )


@router.post("/trivia/decks/{deck_id}/generate", response_class=HTMLResponse)
def trivia_generate(
    deck_id: int,
    request: Request,
    user: dict = Depends(current_user),
):
    """Synchronously generate a fresh batch for `deck_id`. Used as the
    "Generate batch" button on the deck page and as the implicit
    fallback the scheduler runs when a deck's queue is empty.

    Redirects back to the deck page on success; raises a 502-ish
    error page if the agent is unreachable.
    """
    decks = DeckRepo()
    questions = QuestionRepo()
    trivia = TriviaQueueRepo()
    # DeckRepo.find_name scopes by user_id, so it doubles as the IDOR
    # guard — wrong-user lookups return None and we 404 the same as
    # "no such deck."
    deck_name = decks.find_name(user["tailscale_login"], deck_id)
    if deck_name is None:
        raise HTTPException(404, "deck not found")
    # Use the deck's context_prompt as the trivia topic. (Trivia decks
    # set this at creation; SRS decks shouldn't be hitting this route.)
    topic = decks.get_context_prompt(user["tailscale_login"], deck_name) or deck_name
    try:
        outcome = trivia_service.generate_batch(
            user_id=user["tailscale_login"],
            deck_id=deck_id,
            topic=topic,
            questions_repo=questions,
            trivia_repo=trivia,
        )
    except AgentUnavailable as e:
        raise HTTPException(502, f"trivia generation failed: {e}") from e

    root = request.scope.get("root_path", "") or ""
    return HTMLResponse(
        f"""<!doctype html><meta charset="utf-8">
<meta http-equiv="refresh" content="0; url={root}/deck/{deck_name}">
<p>Generated {outcome.inserted} new questions
(skipped {outcome.skipped_duplicates} duplicates,
{outcome.skipped_invalid} invalid).
<a href="{root}/deck/{deck_name}">Back to deck</a>.</p>""",
        status_code=200,
    )
