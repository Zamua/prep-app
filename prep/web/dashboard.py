"""The dashboard's server surface behind the shared components.

Two endpoints, both feeding static/js/dashboard/:

- `GET /api/dashboard/overview`: JSON, shaped to the DeckSource
  contract in static/js/dashboard/source.js. The signed-in dashboard
  route embeds the same payload in its shell for the first paint, so
  both readings come from `overview_payload` and cannot drift.
- `GET /api/dashboard/deck-menus`: the per-deck overflow menus as
  markup. Every row of that menu is a server route, so the server
  composes it and the host mounts it through the components'
  `deckMenu` seam rather than re-implementing the rows in JS.

The repo calls are the ones the dashboard route already makes: deck
summaries, per-trivia-deck stats, and the next future due date. No
query lives here that the HTML route does not also run.

`unsynced` is null: an outbox is a client-side store, so the server
has nothing to answer with. That is the contract's "this surface
cannot say", not a claim of zero.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from prep.auth import current_user
from prep.decks.entities import DeckSummary, DeckType
from prep.decks.repo import DeckRepo
from prep.study.repo import ReviewRepo
from prep.trivia.repo import TriviaQueueRepo
from prep.web.templates import templates

router = APIRouter()


def overview_payload(
    user: dict,
    summaries: list[DeckSummary] | None = None,
    *,
    deck_repo: DeckRepo | None = None,
    trivia_repo: TriviaQueueRepo | None = None,
    review_repo: ReviewRepo | None = None,
) -> dict:
    """The DeckSource overview for one user.

    The raw account id is deliberately absent: an anonymous account's
    id is the one identifier that must never reach a rendered page,
    and no view needs it.
    """
    uid = user["tailscale_login"]
    if summaries is None:
        summaries = (deck_repo or DeckRepo()).list_summaries(uid)
    trivia_repo = trivia_repo or TriviaQueueRepo()
    review_repo = review_repo or ReviewRepo()
    decks = [
        {
            "id": d.id,
            "slug": d.name,
            "display_name": d.display,
            "due": d.due,
            "total": d.total,
            "deck_type": d.deck_type.value,
            "pinned": d.pinned,
            "trivia_stats": (
                trivia_repo.deck_stats(d.id) if d.deck_type == DeckType.TRIVIA else None
            ),
        }
        for d in summaries
    ]
    return {
        "user": {
            "display_name": user.get("display_name") or None,
            "is_anonymous": bool(user.get("is_anonymous")),
        },
        "decks": decks,
        "due": sum(d["due"] for d in decks),
        "total": sum(d["total"] for d in decks),
        "nextDueMinutes": review_repo.next_due_minutes(uid),
        "unsynced": None,
    }


def menu_context(request: Request, summaries: list[DeckSummary]) -> dict:
    """Template context for partials/deck_menus.html. The rows read
    the same summary dicts the deck list is built from, so a menu can
    never describe a deck the list does not show."""
    return {"request": request, "menu_decks": [d.model_dump() for d in summaries]}


@router.get("/api/dashboard/overview", include_in_schema=False)
def dashboard_overview(user: dict = Depends(current_user)) -> JSONResponse:
    """Re-read of the payload the dashboard shell embeds. The host
    calls it after an action that changes the list (a pin toggle), so
    the re-render comes from the server's counts rather than from a
    guess about what the action did."""
    return JSONResponse(overview_payload(user))


@router.get("/api/dashboard/deck-menus", response_class=HTMLResponse, include_in_schema=False)
def dashboard_deck_menus(
    request: Request,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(DeckRepo),
) -> HTMLResponse:
    """The overflow menus, re-rendered. Fetched alongside the overview
    after a pin toggle: the menu's own Pin/Unpin row is server state
    too, and a stale copy would offer the action the user just took."""
    summaries = deck_repo.list_summaries(user["tailscale_login"])
    return templates.TemplateResponse(
        request, "partials/deck_menus.html", menu_context(request, summaries)
    )
