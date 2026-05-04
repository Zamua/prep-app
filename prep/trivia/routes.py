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

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from prep.auth import current_user
from prep.decks.repo import DeckRepo, QuestionRepo
from prep.trivia import service as trivia_service
from prep.trivia.agent_client import AgentUnavailable
from prep.trivia.repo import TriviaQueueRepo, TriviaSessionsRepo
from prep.trivia.service import build_explore_ctx, grade_with_fallback
from prep.trivia.session_state import (
    flip_done_verdict,
    format_done,
    parse_card_ids,
    parse_done,
)
from prep.web import responses
from prep.web.templates import templates

logger = logging.getLogger(__name__)
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


# ---- plan-review signals (replan / accept / reject) -------------------


def _verify_trivia_wid_owned_by(user: dict, wid: str) -> str:
    """Helper: parse the wid, IDOR-check that the user owns the deck.
    Returns the deck_name. Raises HTTPException on either failure."""
    deck_name = _parse_trivia_wid(wid)
    if not deck_name:
        raise HTTPException(400, "malformed workflow id")
    if DeckRepo().find_id(user["tailscale_login"], deck_name) is None:
        raise HTTPException(404, "deck not found")
    return deck_name


@router.post("/trivia/gen/{wid}/feedback")
async def trivia_generating_feedback(
    wid: str, request: Request, user: dict = Depends(current_user)
):
    """Send free-text feedback to the trivia workflow's awaiting_feedback
    step. Workflow replans using the prior plan + this feedback."""
    from prep import temporal_client

    _verify_trivia_wid_owned_by(user, wid)
    form = await request.form()
    feedback = (form.get("feedback") or "").strip()
    if not feedback:
        raise HTTPException(400, "feedback required")
    await temporal_client.signal_trivia_feedback(wid, feedback)
    return JSONResponse({"ok": True})


@router.post("/trivia/gen/{wid}/accept")
async def trivia_generating_accept(wid: str, user: dict = Depends(current_user)):
    """Accept the current plan; workflow moves to parallel expansion."""
    from prep import temporal_client

    _verify_trivia_wid_owned_by(user, wid)
    await temporal_client.signal_trivia_accept(wid)
    return JSONResponse({"ok": True})


@router.post("/trivia/gen/{wid}/reject")
async def trivia_generating_reject(wid: str, user: dict = Depends(current_user)):
    """Bail on the workflow without writing any cards."""
    from prep import temporal_client

    _verify_trivia_wid_owned_by(user, wid)
    await temporal_client.signal_trivia_reject(wid)
    return JSONResponse({"ok": True})


# ---- mini-session (notification target) -------------------------------
#
# Notifications deep-link to /trivia/session/<deck_name>. The session
# is stateless: a comma-separated `cards` query param holds the
# remaining queue. Each answer pops the head and redirects to the
# next, so refresh / back-button behavior stays sensible. When the
# list empties, the summary view renders.


@router.get("/trivia/session/{deck_name}", response_class=HTMLResponse)
def trivia_session(
    deck_name: str,
    request: Request,
    cards: str | None = None,
    done: str | None = None,
    user: dict = Depends(current_user),
):
    """Notification deep-link target: a 3-card mini-session. With no
    `cards` param, server picks a fresh session and redirects with the
    queue encoded. With an empty `cards`, renders the summary. Otherwise
    renders the head card; submit pops the head and redirects.

    The `done` param accumulates per-card verdicts as the user
    progresses (`<qid><r|w>,...`) so the summary view at the end can
    render the run without server-side session state."""
    uid = user["tailscale_login"]
    decks = DeckRepo()
    questions = QuestionRepo()
    trivia = TriviaQueueRepo()
    sessions = TriviaSessionsRepo()
    deck_id = decks.find_id(uid, deck_name)
    if deck_id is None:
        raise HTTPException(404, "deck not found")

    if cards is None:
        # No URL state — either a manual nav to the bare session URL,
        # or a stale notification log entry. If there's an active
        # persisted session for this deck, resume it (canonical URL
        # rebuilt from DB). Otherwise pick a fresh queue + persist.
        active = sessions.get_active_for_deck(uid, deck_id)
        if active and active.queue:
            done_qs = f"&done={format_done(active.done)}" if active.done else ""
            ids = ",".join(str(q) for q in active.queue)
            return responses.redirect(request, f"/trivia/session/{deck_name}?cards={ids}{done_qs}")
        target_size = decks.get_trivia_session_size(uid, deck_id)
        # Half fresh, half review (rounded down, min 1). For
        # target_size=10 that's 5/5; for 3 it's 1/2. Generate first
        # if the truly-fresh pool can't satisfy the half. Synchronous
        # by design — the user just tapped a notification and is
        # waiting to study; better to delay the session ~10s for fresh
        # content than to serve a session that's mostly review.
        fresh_target = max(1, target_size // 2)
        if trivia.count_unanswered(deck_id) < fresh_target:
            topic = (decks.get_context_prompt(uid, deck_name) or deck_name).strip()
            if topic:
                try:
                    trivia_service.generate_batch(
                        user_id=uid,
                        deck_id=deck_id,
                        topic=topic,
                        questions_repo=questions,
                        trivia_repo=trivia,
                    )
                except AgentUnavailable as e:
                    logger.warning(
                        "session refill failed for deck %s: %s — proceeding with what's there",
                        deck_id,
                        e,
                    )
        session = trivia.pick_session_for_deck(
            deck_id, target_size=target_size, fresh_target=fresh_target
        )
        picked_ids = [c.question_id for c in session]
        # Replace any stale active session with the freshly picked
        # queue (silent — at this point any prior session was either
        # completed, abandoned, or empty).
        sessions.replace_active(uid, deck_id, queue=picked_ids)
        ids = ",".join(str(qid) for qid in picked_ids)
        return responses.redirect(request, f"/trivia/session/{deck_name}?cards={ids}")

    queue = parse_card_ids(cards)
    done_items = parse_done(done)
    # Mid-session hit — keep the persistence row in sync with the URL
    # state. start_or_resume is a no-op refresh if a row already
    # exists; otherwise it creates one matching the current URL.
    if queue:
        sessions.start_or_resume(uid, deck_id, queue=queue, done=done_items)
    if not queue:
        # End of session — mark the persisted row as completed so it
        # stops showing in the index "Continue" strip + the scheduler
        # picks fresh on the next tick.
        sessions.complete(uid, deck_id)
        # Render summary: hydrate each done entry with its question
        # text + correct answer + explanation for the tap-to-expand
        # detail panels.
        results = []
        for qid, verdict in done_items:
            q = questions.get(uid, qid)
            if q is None:
                continue
            results.append(
                {
                    "id": q.id,
                    "prompt": q.prompt,
                    "answer": q.answer,
                    "explanation": q.explanation,
                    "verdict": verdict,
                }
            )
        right_count = sum(1 for r in results if r["verdict"] == "r")
        return templates.TemplateResponse(
            request,
            "trivia/session_done.html",
            {
                "deck_name": deck_name,
                "results": results,
                "right_count": right_count,
                "total": len(results),
            },
        )

    head = queue[0]
    q = questions.get(uid, head)
    if q is None or q.deck_id != deck_id:
        # Skip cards the user can't access (stale URL after a deck
        # delete, or someone trying to inject foreign question_ids).
        remaining = ",".join(str(i) for i in queue[1:])
        done_qs = f"&done={done}" if done else ""
        return responses.redirect(
            request, f"/trivia/session/{deck_name}?cards={remaining}{done_qs}"
        )
    total = len(done_items) + len(queue)
    position = len(done_items) + 1
    return templates.TemplateResponse(
        request,
        "trivia/card.html",
        {
            "q": q,
            "deck_name": deck_name,
            "result": None,
            "session_position": position,
            "session_total": total,
            "session_remaining": cards,
            "session_done": done or "",
        },
    )


@router.post("/trivia/session/{deck_name}/answer", response_class=HTMLResponse)
def trivia_session_answer(
    deck_name: str,
    request: Request,
    cards: str = Form(""),
    done: str = Form(""),
    answer: str = Form(""),
    idk: str = Form(""),
    user: dict = Depends(current_user),
):
    """Grade the head card, mark_answered, render the result panel.
    Appends `<head><r|w>` to the `done` chain so the next-card link
    (and eventually the summary view) carries the verdict forward.

    `idk=1` is the "I don't know" submit — skip grading entirely and
    record as wrong. `formnovalidate` on the button bypasses the
    answer field's `required`, so an empty input still POSTs."""
    uid = user["tailscale_login"]
    decks = DeckRepo()
    questions = QuestionRepo()
    trivia = TriviaQueueRepo()
    deck_id = decks.find_id(uid, deck_name)
    if deck_id is None:
        raise HTTPException(404, "deck not found")

    queue = parse_card_ids(cards)
    done_items = parse_done(done)
    if not queue:
        done_qs = f"&done={done}" if done else ""
        return responses.redirect(request, f"/trivia/session/{deck_name}?cards={done_qs}")

    head = queue[0]
    q = questions.get(uid, head)
    if q is None or q.deck_id != deck_id:
        # Stale / foreign card — pop and continue.
        remaining = ",".join(str(i) for i in queue[1:])
        done_qs = f"&done={done}" if done else ""
        return responses.redirect(
            request, f"/trivia/session/{deck_name}?cards={remaining}{done_qs}"
        )

    is_idk = bool(idk)
    if is_idk:
        correct = False
        verdict: dict = {"correct": False, "feedback": None, "regex_update": None}
        given = ""
    else:
        verdict = grade_with_fallback(q, answer)
        correct = verdict["correct"]
        given = answer

    trivia.mark_answered(head, correct=correct)
    regex_updated = False
    if verdict.get("regex_update"):
        regex_updated = questions.set_answer_regex(uid, head, verdict["regex_update"])

    new_done_items = done_items + [(head, "r" if correct else "w")]
    new_done_str = format_done(new_done_items)
    remaining_ids = queue[1:]
    remaining = ",".join(str(i) for i in remaining_ids)
    # Persist the post-answer queue + done back to the session row so
    # a tab close right here lets the user resume the next card.
    TriviaSessionsRepo().persist_state(uid, deck_id, queue=remaining_ids, done=new_done_items)
    # Position counter on the result view stays on the card the user
    # just answered — counter rolls UP across the session
    # (1/3 → 2/3 → 3/3) instead of down.
    position = len(done_items) + 1
    total = len(done_items) + len(queue)
    return templates.TemplateResponse(
        request,
        "trivia/card.html",
        {
            "q": q,
            "deck_name": deck_name,
            "result": {
                "correct": correct,
                "given": given,
                "expected": q.answer,
                "feedback": verdict.get("feedback"),
                "idk": is_idk,
                "regex_updated": regex_updated,
            },
            "session_position": position,
            "session_total": total,
            "session_remaining": remaining,
            "session_done": new_done_str,
            **build_explore_ctx(
                deck_name=deck_name,
                q=q,
                user_answer=given,
                correct=correct,
                expected=q.answer,
                idk=is_idk,
            ),
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

    verdict = grade_with_fallback(q, answer)
    correct = verdict["correct"]
    trivia.mark_answered(question_id, correct=correct)
    regex_updated = False
    if verdict.get("regex_update"):
        regex_updated = questions.set_answer_regex(
            user["tailscale_login"], question_id, verdict["regex_update"]
        )
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
                "feedback": verdict.get("feedback"),
                "regex_updated": regex_updated,
            },
            **build_explore_ctx(
                deck_name=deck_name or "",
                q=q,
                user_answer=answer,
                correct=correct,
                expected=q.answer,
            ),
        },
    )


@router.post("/trivia/{question_id}/regrade", response_class=HTMLResponse)
def trivia_regrade(
    question_id: int,
    request: Request,
    answer: str = Form(""),
    user: dict = Depends(current_user),
):
    """Force the claude grader to re-evaluate the user's answer for a
    standalone (non-session) trivia card. Updates the recorded verdict
    if it flips and re-renders the result panel. The card isn't
    re-rotated — `set_last_correctness` only touches the verdict
    column."""
    questions = QuestionRepo()
    decks = DeckRepo()
    trivia = TriviaQueueRepo()

    q = questions.get(user["tailscale_login"], question_id)
    if q is None:
        raise HTTPException(404, "question not found")

    verdict = trivia_service.claude_regrade(
        prompt=q.prompt,
        expected=q.answer,
        given=answer,
        current_regex=q.answer_regex,
    )
    correct = verdict["correct"]
    trivia.set_last_correctness(question_id, correct=correct)
    regex_updated = False
    if verdict.get("regex_update"):
        regex_updated = questions.set_answer_regex(
            user["tailscale_login"], question_id, verdict["regex_update"]
        )

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
                "feedback": verdict.get("feedback"),
                "regraded": True,
                "regex_updated": regex_updated,
            },
            **build_explore_ctx(
                deck_name=deck_name or "",
                q=q,
                user_answer=answer,
                correct=correct,
                expected=q.answer,
            ),
        },
    )


@router.post("/trivia/session/{deck_name}/regrade", response_class=HTMLResponse)
def trivia_session_regrade(
    deck_name: str,
    request: Request,
    question_id: int = Form(...),
    cards: str = Form(""),
    done: str = Form(""),
    answer: str = Form(""),
    user: dict = Depends(current_user),
):
    """Session variant of the regrade route. In addition to flipping
    the recorded verdict, mutates the `done` chain so the next-card
    link (and the summary view at the end) reflects the new verdict
    for this question."""
    uid = user["tailscale_login"]
    decks = DeckRepo()
    questions = QuestionRepo()
    trivia = TriviaQueueRepo()
    deck_id = decks.find_id(uid, deck_name)
    if deck_id is None:
        raise HTTPException(404, "deck not found")

    q = questions.get(uid, question_id)
    if q is None or q.deck_id != deck_id:
        raise HTTPException(404, "question not found in this deck")

    verdict = trivia_service.claude_regrade(
        prompt=q.prompt,
        expected=q.answer,
        given=answer,
        current_regex=q.answer_regex,
    )
    correct = verdict["correct"]
    trivia.set_last_correctness(question_id, correct=correct)
    regex_updated = False
    if verdict.get("regex_update"):
        regex_updated = questions.set_answer_regex(uid, question_id, verdict["regex_update"])

    done_items = parse_done(done)
    new_done_str = flip_done_verdict(done_items, question_id, correct)
    # Position counter on the result view stays on the just-graded card.
    queue = parse_card_ids(cards)
    position = max(1, len(done_items))
    total = len(done_items) + len(queue)

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
                "feedback": verdict.get("feedback"),
                "regraded": True,
                "regex_updated": regex_updated,
            },
            "session_position": position,
            "session_total": total,
            "session_remaining": cards,
            "session_done": new_done_str,
            **build_explore_ctx(
                deck_name=deck_name,
                q=q,
                user_answer=answer,
                correct=correct,
                expected=q.answer,
            ),
        },
    )


@router.post("/trivia/decks/{deck_id}/notifications")
def trivia_toggle_notifications(
    deck_id: int,
    request: Request,
    enabled: str = Form(...),
    user: dict = Depends(current_user),
):
    """Flip the per-deck notification cycle on or off. 404 if the
    deck doesn't belong to the user. Returns a 303 (no meta-refresh
    interstitial — the meta-refresh chrome was the source of the
    white-flash on toggle)."""
    decks = DeckRepo()
    if not decks.set_notifications_enabled(user["tailscale_login"], deck_id, enabled == "on"):
        raise HTTPException(404, "trivia deck not found")
    deck_name = decks.find_name(user["tailscale_login"], deck_id) or ""
    return responses.redirect(request, f"/deck/{deck_name}")


@router.post("/trivia/decks/{deck_id}/interval")
def trivia_set_interval(
    deck_id: int,
    request: Request,
    minutes: str = Form(...),
    user: dict = Depends(current_user),
):
    """Update the deck's base notification interval. Form input is
    plain-text minutes (1..720); rejects garbage with 400, IDOR-guards
    via user-scoped repo. 303s back to the deck page so the rendered
    pill picks up the new value."""
    try:
        m = int(minutes)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, "interval must be an integer (minutes)") from e
    if m < 1 or m > 720:
        raise HTTPException(400, "interval must be between 1 and 720 minutes")
    decks = DeckRepo()
    if not decks.set_notification_interval(user["tailscale_login"], deck_id, m):
        raise HTTPException(404, "trivia deck not found")
    deck_name = decks.find_name(user["tailscale_login"], deck_id) or ""
    return responses.redirect(request, f"/deck/{deck_name}")


@router.post("/trivia/decks/{deck_id}/session_size")
def trivia_set_session_size(
    deck_id: int,
    request: Request,
    size: str = Form(...),
    user: dict = Depends(current_user),
):
    """Update the deck's mini-session card count (1..20). Form input is
    plain text; rejects garbage with 400, IDOR-guards via user-scoped
    repo. 303s back to the deck page so the popover re-renders with the
    new active preset."""
    try:
        n = int(size)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, "session size must be an integer") from e
    if n < 1 or n > 20:
        raise HTTPException(400, "session size must be between 1 and 20 cards")
    decks = DeckRepo()
    if not decks.set_trivia_session_size(user["tailscale_login"], deck_id, n):
        raise HTTPException(404, "trivia deck not found")
    deck_name = decks.find_name(user["tailscale_login"], deck_id) or ""
    return responses.redirect(request, f"/deck/{deck_name}")


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
