"""Public REST API at /api/v1/* + the /settings/api management UI.

API surface (v1):
- GET    /api/v1/decks                       → list user's decks
- POST   /api/v1/decks                       → create deck { name, type?, context_prompt? }
- GET    /api/v1/decks/{name}                → deck metadata
- GET    /api/v1/decks/{name}/cards          → list cards (JSON)
- GET    /api/v1/decks/{name}/export.csv     → download CSV
- POST   /api/v1/decks/{name}/import-csv     → text/csv body, returns ImportOutcome JSON

UI surface (cookie-authed):
- GET  /settings/api                         → token management page
- POST /settings/api/tokens                  → mint a new token
- POST /settings/api/tokens/{id}/delete      → revoke

Wire format for cards mirrors prep.decks.io.CSV_COLUMNS so the JSON
view + the CSV view are interchangeable. The same Pydantic model
backs both.
"""

from __future__ import annotations

import logging
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from prep.api.auth import bearer_user
from prep.api.entities import ApiTokenMetadata
from prep.api.mcp import router as mcp_router
from prep.api.repo import ApiTokenRepo
from prep.auth import current_user
from prep.decks.io import csv_to_deck, deck_to_csv
from prep.decks.repo import DeckRepo, QuestionRepo
from prep.web.templates import templates

logger = logging.getLogger(__name__)
router = APIRouter()

# Mount the MCP-over-HTTP server (POST /mcp) as a child router. Same
# bearer-token auth; same per-user repo scoping. Lives in its own
# module so the JSON-RPC plumbing doesn't clutter routes.py.
router.include_router(mcp_router)


# ---- /settings/api — management UI -------------------------------------


def _render_api_settings(
    request: Request, user: dict, *, created_plaintext: str | None = None, flash: str | None = None
):
    tokens = ApiTokenRepo().list_for_user(user["tailscale_login"])
    return templates.TemplateResponse(
        "settings_api.html",
        {
            "request": request,
            "user": user,
            "tokens": tokens,
            "created_plaintext": created_plaintext,
            "flash": flash,
        },
    )


@router.get("/settings/api", response_class=HTMLResponse)
def settings_api(request: Request, user: dict = Depends(current_user)):
    return _render_api_settings(request, user)


@router.post("/settings/api/tokens", response_class=HTMLResponse)
async def settings_api_create(request: Request, user: dict = Depends(current_user)):
    """Mint a token + render it inline on this response. The plaintext
    is NEVER persisted to a session, NEVER put in a query string (would
    leak into nginx access logs + browser history + Referer headers
    on outbound clicks), and NEVER returned by a GET. Refresh the page
    → it's gone."""
    form = await request.form()
    label = (form.get("label") or "").strip() or None
    token, _meta = ApiTokenRepo().issue(user_id=user["tailscale_login"], label=label)
    return _render_api_settings(request, user, created_plaintext=token)


@router.post("/settings/api/tokens/{token_id}/delete", response_class=HTMLResponse)
def settings_api_delete(token_id: int, request: Request, user: dict = Depends(current_user)):
    ApiTokenRepo().delete(user_id=user["tailscale_login"], token_id=token_id)
    return _render_api_settings(request, user, flash="Token revoked.")


# ---- /api/v1 — public REST surface -------------------------------------


class _DeckSummaryJson(BaseModel):
    name: str
    type: str
    card_count: int
    due: int = 0
    pinned: bool = False


class _CardJson(BaseModel):
    type: str
    topic: str | None = None
    prompt: str
    answer: str
    choices: list[str] | None = None
    rubric: str | None = None
    skeleton: str | None = None
    language: str | None = None
    answer_regex: str | None = None
    explanation: str | None = None


class _NewDeckBody(BaseModel):
    name: str = Field(min_length=2, max_length=30)
    context_prompt: str | None = None


@router.get("/api/v1/decks")
def api_list_decks(user: dict = Depends(bearer_user)):
    uid = user["tailscale_login"]
    summaries = DeckRepo().list_summaries(uid)
    out = [
        _DeckSummaryJson(
            name=s.name,
            type=s.deck_type.value if s.deck_type else "srs",
            card_count=s.total,
            due=s.due,
            pinned=s.pinned,
        )
        for s in summaries
    ]
    return {"decks": [d.model_dump() for d in out]}


@router.post("/api/v1/decks")
def api_create_deck(body: _NewDeckBody, user: dict = Depends(bearer_user)):
    uid = user["tailscale_login"]
    repo = DeckRepo()
    if repo.find_id(uid, body.name) is not None:
        raise HTTPException(409, f"deck {body.name!r} already exists")
    deck_id = repo.create(uid, body.name, body.context_prompt)
    return {"name": body.name, "id": deck_id}


@router.get("/api/v1/decks/{name}")
def api_deck_meta(name: str, user: dict = Depends(bearer_user)):
    uid = user["tailscale_login"]
    repo = DeckRepo()
    deck_id = repo.find_id(uid, name)
    if deck_id is None:
        raise HTTPException(404, "deck not found")
    meta = repo.get_meta(uid, deck_id)
    deck_type = repo.get_type(uid, deck_id)
    cards = QuestionRepo().list_in_deck(uid, deck_id)
    return {
        "name": name,
        "type": deck_type.value if deck_type else "srs",
        "context_prompt": meta.context_prompt,
        "card_count": len(cards),
    }


@router.get("/api/v1/decks/{name}/cards")
def api_list_cards(name: str, user: dict = Depends(bearer_user)):
    """List every card in the deck. Same field set the CSV exporter
    emits — JSON is the agent-friendly shape, CSV is the spreadsheet-
    friendly shape, both backed by prep.decks.io."""
    uid = user["tailscale_login"]
    deck_id = DeckRepo().find_id(uid, name)
    if deck_id is None:
        raise HTTPException(404, "deck not found")
    # Reuse the io module's full-question fetch so JSON/CSV agree on
    # field set.
    from prep.decks.io import _questions_for_export

    questions = _questions_for_export(uid, deck_id)
    cards = [
        _CardJson(
            type=q.type.value,
            topic=q.topic,
            prompt=q.prompt,
            answer=q.answer,
            choices=q.choices,
            rubric=q.rubric,
            skeleton=q.skeleton,
            language=q.language,
            answer_regex=q.answer_regex,
            explanation=q.explanation,
        )
        for q in questions
    ]
    return {"deck": name, "cards": [c.model_dump() for c in cards]}


@router.get("/api/v1/decks/{name}/export.csv")
def api_deck_export_csv(name: str, user: dict = Depends(bearer_user)):
    uid = user["tailscale_login"]
    deck_id = DeckRepo().find_id(uid, name)
    if deck_id is None:
        raise HTTPException(404, "deck not found")
    body = deck_to_csv(uid, deck_id)
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
    )


@router.post("/api/v1/decks/{name}/import-csv")
async def api_deck_import_csv(name: str, request: Request, user: dict = Depends(bearer_user)):
    """Append CSV rows to `name` (creates the deck if missing). Body
    must be text/csv. Returns an ImportOutcome JSON: inserted /
    skipped_duplicates / errors."""
    raw = await request.body()
    try:
        csv_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        csv_text = raw.decode("utf-8", errors="replace")
    if not csv_text.strip():
        raise HTTPException(400, "empty CSV body")
    uid = user["tailscale_login"]
    outcome = csv_to_deck(
        uid,
        name,
        csv_text,
        deck_repo=DeckRepo(),
        question_repo=QuestionRepo(),
    )
    return JSONResponse(asdict(outcome))


# ---- helpers ----------------------------------------------------------


def _serialize_token_metadata(m: ApiTokenMetadata) -> dict:
    """Public-safe rep of an ApiTokenMetadata — used if we expose a
    JSON list-tokens endpoint later. Kept here so the wire shape lives
    next to the route layer."""
    return {
        "id": m.id,
        "label": m.label,
        "key_prefix": m.key_prefix,
        "created_at": m.created_at,
        "last_used_at": m.last_used_at,
    }
