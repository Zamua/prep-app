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
from prep.decks.repo import DeckRepo
from prep.web import responses

router = APIRouter()


def _deck_repo() -> DeckRepo:
    """Per-request DeckRepo. Cheap to instantiate (it's stateless —
    just delegates to module-level db functions). FastAPI's
    Depends() caches it per-request so the wiring is uniform across
    routes that need it."""
    return DeckRepo()


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
