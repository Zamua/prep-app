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
from prep.trivia.repo import TriviaQueueRepo, TriviaSessionsRepo
from prep.trivia.session_state import format_done
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
    # Trivia decks need extra stats for the mini mastery bar — total /
    # mastered / wrong / unanswered. SRS decks use the existing due/total
    # rendering and don't need this. One query per trivia deck is fine
    # at this scale (a single user has tens of decks at most).
    trivia_repo = TriviaQueueRepo()
    deck_dicts = []
    for d in decks:
        item = d.model_dump()
        if d.deck_type == d.deck_type.TRIVIA:
            item["trivia_stats"] = trivia_repo.deck_stats(d.id)
        deck_dicts.append(item)
    # Active trivia sessions across all decks — powers the "Continue"
    # strip at the top of the home page so the user can resume any
    # in-progress session without going to the deck page first.
    active_trivia = TriviaSessionsRepo().list_active(uid)
    active_trivia_views = [
        {
            "deck_name": s.deck_name,
            "remaining": s.remaining,
            "total": s.total,
            "last_active": s.last_active,
            "queue_param": ",".join(str(q) for q in s.queue),
            "done_param": format_done(s.done),
        }
        for s in active_trivia
    ]
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "user": user,
            "decks": deck_dicts,
            "recent_sessions": [r.model_dump() for r in recents],
            "active_trivia_sessions": active_trivia_views,
        },
    )
