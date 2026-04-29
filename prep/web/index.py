"""Index / home page route.

Cross-cuts the decks and study contexts (lists user's decks alongside
their recent study sessions), so it lives at the prep/web/ level
rather than under either context's routes module.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from prep.auth import current_user
from prep.decks.repo import DeckRepo
from prep.study.repo import SessionRepo
from prep.web.templates import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(DeckRepo),
    session_repo: SessionRepo = Depends(SessionRepo),
):
    """Home page: the user's decks (sorted by name) plus the last
    five active study sessions across all decks."""
    uid = user["tailscale_login"]
    decks = sorted(deck_repo.list_summaries(uid), key=lambda d: d.name)
    recents = session_repo.list_recent(uid, limit=5)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "user": user,
            # Templates iterate raw dicts; expose the entity fields
            # directly via .model_dump() to keep the template free of
            # pydantic-specific access patterns.
            "decks": [d.model_dump() for d in decks],
            "recent_sessions": [r.model_dump() for r in recents],
        },
    )
