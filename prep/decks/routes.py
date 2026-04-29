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

Currently extracted: /deck/{name}/delete. Subsequent commits will
move the rest of the deck/question routes here as the pattern
proves out — full move covered by phase 5d's follow-on commits.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from prep.auth import current_user
from prep.decks import service
from prep.decks.repo import DeckRepo, QuestionRepo
from prep.web import responses

router = APIRouter()


def _deck_repo() -> DeckRepo:
    """Per-request DeckRepo. Cheap to instantiate (it's stateless —
    just delegates to module-level db functions). FastAPI's
    Depends() caches it per-request so the wiring is uniform across
    routes that need it."""
    return DeckRepo()


def _question_repo() -> QuestionRepo:
    return QuestionRepo()


# ---- Deck-level routes --------------------------------------------------


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
