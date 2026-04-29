"""HTTP routes for the decks bounded context.

Each handler is a thin translation layer:
  parse request → call service → render response.

No SQL, no temporal-client calls — those live behind the service /
repo / temporal-client modules respectively. Routes get an
APIRouter that app.py mounts at module load time.

Why a Router and not @app.get(): keeps each context's HTTP surface
in one place (this file is the contract for the decks UI), keeps
app.py thin, and makes it trivial to remount under a prefix later
if we ever expose deck routes under /api/v1/decks/* alongside the
HTML routes.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from prep import db
from prep.auth import current_user
from prep.decks import service
from prep.decks.repo import DeckRepo, QuestionRepo
from prep.web import responses
from prep.web.templates import templates

router = APIRouter()


# ---- workflow-id parsing ------------------------------------------------
#
# Workflow IDs are constructed by prep.temporal_client when starting a
# workflow. Their shapes are stable strings — the route layer parses
# them to recover the resource being operated on (deck or question)
# for ownership checks.


def _parse_transform_wid(wid: str) -> tuple[str, int] | None:
    """`transform-<scope>-<target_id>-<rand>`. Returns (scope, target_id)
    or None if malformed. scope ∈ {card, deck}."""
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


def _parse_plan_wid(wid: str) -> str | None:
    """`plan-<deck_name>-<rand>`. Returns deck_name or None.

    deck_name may itself contain hyphens, so we split on the *trailing*
    rand suffix (last hyphen) and treat what's left as the name."""
    if not wid.startswith("plan-"):
        return None
    rest = wid[len("plan-") :]
    if "-" not in rest:
        return None
    name, _, suffix = rest.rpartition("-")
    if not name or len(suffix) < 6:
        return None
    return name


def _deck_repo() -> DeckRepo:
    """Per-request DeckRepo. Cheap to instantiate (it's stateless —
    just delegates to module-level db functions). FastAPI's
    Depends() caches it per-request so the wiring is uniform across
    routes that need it."""
    return DeckRepo()


def _question_repo() -> QuestionRepo:
    return QuestionRepo()


# ---- Deck-level routes --------------------------------------------------


@router.get("/deck/{name}", response_class=HTMLResponse)
def deck_view(
    request: Request,
    name: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    """Show every question in a deck (suspended + active), with
    inline SRS state + the deck's free-form context_prompt. Uses
    `get_or_create_deck` so first-time navigation lazily materializes
    a deck row even if the user hasn't added any questions yet."""
    uid = user["tailscale_login"]
    deck_id = deck_repo.get_or_create(uid, name)
    cards = q_repo.list_in_deck(uid, deck_id)
    now_ts = db.now()
    return templates.TemplateResponse(
        "deck.html",
        {
            "request": request,
            "user": user,
            "deck_name": name,
            "questions": cards,
            "due_count": sum(
                1 for c in cards if not c.suspended and c.next_due and c.next_due <= now_ts
            ),
        },
    )


@router.post("/deck/{name}/delete")
def deck_delete(
    request: Request,
    name: str,
    confirm: str = Form(...),
    user: dict = Depends(current_user),
    repo: DeckRepo = Depends(_deck_repo),
):
    """Delete a deck and (via FK CASCADE) all its questions/cards/
    reviews/sessions. Requires the user to type the deck name into a
    `confirm` field on the dialog form — guards against accidental
    clicks. Returns a redirect back to the index."""
    uid = user["tailscale_login"]
    if confirm.strip() != name:
        raise HTTPException(400, "deck name didn't match — delete not performed")
    if repo.find_id(uid, name) is None:
        raise HTTPException(404, "deck not found")
    service.delete_deck(repo, uid, name)
    return responses.redirect(request, "/")


# ---- Question-level routes ----------------------------------------------


@router.post("/question/{qid}/suspend")
def question_suspend(
    request: Request,
    qid: int,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    """Toggle a question into the suspended state — it stops appearing
    in study sessions but stays in the deck. Redirects back to the
    deck page so the toggle in the UI can update inline."""
    uid = user["tailscale_login"]
    q = q_repo.get(uid, qid)
    if q is None:
        raise HTTPException(404, "question not found")
    service.suspend_question(q_repo, uid, qid)
    deck_name = deck_repo.find_name(uid, q.deck_id)
    return responses.redirect(request, f"/deck/{deck_name}")


@router.post("/question/{qid}/unsuspend")
def question_unsuspend(
    request: Request,
    qid: int,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    """Inverse of suspend — re-include the question in study sessions."""
    uid = user["tailscale_login"]
    q = q_repo.get(uid, qid)
    if q is None:
        raise HTTPException(404, "question not found")
    service.unsuspend_question(q_repo, uid, qid)
    deck_name = deck_repo.find_name(uid, q.deck_id)
    return responses.redirect(request, f"/deck/{deck_name}")


@router.post("/question/{qid}/improve")
async def question_improve(
    request: Request,
    qid: int,
    prompt: str = Form(...),
    user: dict = Depends(current_user),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    """Per-card free-text rewrite via the AI agent. Auto-applies on
    completion (card-scope transforms don't have an apply/reject
    review step — that's reserved for deck-wide changes)."""
    uid = user["tailscale_login"]
    if q_repo.get(uid, qid) is None:
        raise HTTPException(404, "question not found")
    if not prompt.strip():
        raise HTTPException(400, "empty prompt")
    # Late import: keeps the routes module's startup-time graph clean
    # of temporal-client setup, which has its own retry/dial logic.
    from prep import temporal_client

    try:
        result = await service.start_card_transform(
            temporal_client,
            user_id=uid,
            qid=qid,
            prompt=prompt.strip(),
        )
    except Exception as e:
        raise HTTPException(500, f"failed to start transform: {e}")
    return responses.redirect(request, f"/transform/{result.workflow_id}")


# ---- transform workflow signals + status -------------------------------


def _require_owns_transform(
    user: dict,
    wid: str,
    deck_repo: DeckRepo,
    q_repo: QuestionRepo,
) -> tuple[str, int]:
    """Parse + verify ownership of a transform workflow id. Routes use
    this as the gate before signaling. Returns (scope, target_id)."""
    parsed = _parse_transform_wid(wid)
    if not parsed:
        raise HTTPException(400, "malformed workflow id")
    scope, target_id = parsed
    uid = user["tailscale_login"]
    if scope == "card":
        if q_repo.get(uid, target_id) is None:
            raise HTTPException(404, "transform not found")
    else:
        if deck_repo.find_name(uid, target_id) is None:
            raise HTTPException(404, "transform not found")
    return scope, target_id


@router.get("/transform/{wid}/status")
async def transform_status(
    wid: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    _require_owns_transform(user, wid, deck_repo, q_repo)
    from prep import temporal_client

    progress = await temporal_client.get_transform_progress(wid)
    desc = await temporal_client.describe_workflow(wid)
    return JSONResponse({"progress": progress, "desc": desc})


@router.post("/transform/{wid}/apply")
async def transform_apply(
    request: Request,
    wid: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    _require_owns_transform(user, wid, deck_repo, q_repo)
    from prep import temporal_client

    try:
        await service.apply_transform(temporal_client, wid)
    except Exception as e:
        raise HTTPException(500, f"signal failed: {e}")
    return responses.redirect(request, f"/transform/{wid}")


@router.post("/transform/{wid}/reject")
async def transform_reject(
    request: Request,
    wid: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    _require_owns_transform(user, wid, deck_repo, q_repo)
    from prep import temporal_client

    try:
        await service.reject_transform(temporal_client, wid)
    except Exception as e:
        raise HTTPException(500, f"signal failed: {e}")
    return responses.redirect(request, f"/transform/{wid}")


# ---- plan-first generation: signals + status ---------------------------


def _require_owns_plan(user: dict, wid: str, deck_repo: DeckRepo) -> tuple[str, int]:
    """Parse + verify ownership of a plan workflow id. Returns
    (deck_name, deck_id)."""
    name = _parse_plan_wid(wid)
    if not name:
        raise HTTPException(400, "malformed workflow id")
    uid = user["tailscale_login"]
    deck_id = deck_repo.find_id(uid, name)
    if deck_id is None:
        raise HTTPException(404, "plan not found")
    return name, deck_id


@router.get("/plan/{wid}/status")
async def plan_status(
    wid: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
):
    """JSON status for the plan polling page. Returns the live progress
    while the workflow is alive, or {"status": "gone"} once the query
    handler is no longer registered."""
    _require_owns_plan(user, wid, deck_repo)
    from prep import temporal_client

    progress = await service.get_plan_progress(temporal_client, wid)
    if progress is None:
        return JSONResponse({"status": "gone"})
    return JSONResponse(progress)


@router.post("/plan/{wid}/feedback")
async def plan_feedback(
    request: Request,
    wid: str,
    feedback: str = Form(...),
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
):
    _require_owns_plan(user, wid, deck_repo)
    if not feedback.strip():
        raise HTTPException(400, "empty feedback")
    from prep import temporal_client

    await service.submit_plan_feedback(temporal_client, wid, feedback.strip())
    return responses.redirect(request, f"/plan/{wid}")


@router.post("/plan/{wid}/accept")
async def plan_accept(
    request: Request,
    wid: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
):
    _require_owns_plan(user, wid, deck_repo)
    from prep import temporal_client

    await service.accept_plan(temporal_client, wid)
    return responses.redirect(request, f"/plan/{wid}")


@router.post("/plan/{wid}/reject")
async def plan_reject(
    request: Request,
    wid: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
):
    name, _ = _require_owns_plan(user, wid, deck_repo)
    from prep import temporal_client

    await service.reject_plan(temporal_client, wid)
    # On reject, return to the deck view rather than the plan page —
    # the plan workflow is being torn down, no use polling it.
    return responses.redirect(request, f"/deck/{name}")
