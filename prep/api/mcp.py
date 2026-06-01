"""Minimal MCP-over-HTTP server.

Implements just enough of the Model Context Protocol spec to let
external AI clients (Claude Desktop, Claude Code, custom scripts) call
prep's deck/card surface as MCP tools. Authenticated via the same
`prep_pat_*` bearer tokens the REST API uses.

## Transport

Single endpoint `POST /mcp` accepting one JSON-RPC 2.0 message per
request. We don't implement Server-Sent Events for server-initiated
messages — every prep tool is synchronous request/response. Clients
that need SSE will hit the simpler POST→JSON path and never miss it.

## Methods implemented

- `initialize` → capability handshake
- `tools/list` → catalog of tools
- `tools/call` → invoke a tool by name + args

Errors land as JSON-RPC error envelopes (code/message). Tool-level
failures (e.g. deck not found) come back with `isError: true` on the
content shape, the convention MCP clients expect.

## Tool surface

- `prep_list_decks` — list the caller's decks
- `prep_get_deck` — deck metadata + card count
- `prep_list_cards` — every card in a deck (JSON)
- `prep_export_deck_csv` — deck contents as CSV text
- `prep_create_deck` — make a new deck
- `prep_import_csv` — append CSV rows to a deck

The same wire shape as the REST API; both backends share
`prep.decks.io` so JSON/CSV stay byte-identical across surfaces.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from prep.api.auth import bearer_user
from prep.decks.io import csv_to_deck, deck_to_csv
from prep.decks.repo import DeckRepo, QuestionRepo

logger = logging.getLogger(__name__)
router = APIRouter()

# MCP spec version this server speaks. Clients negotiate via the
# `initialize` request — they pass their version, we echo a version
# we support back. Bumping this when we adopt newer spec features.
_MCP_PROTOCOL_VERSION = "2025-06-18"
_SERVER_NAME = "prep"
_SERVER_VERSION = "1.0.0"


# ---- tool catalog --------------------------------------------------------


_TOOLS: list[dict[str, Any]] = [
    {
        "name": "prep_list_decks",
        "description": (
            "List every deck owned by the authenticated user. Returns each "
            "deck's name, type (srs|trivia), card count, due count, and "
            "pinned flag. Use prep_get_deck for richer per-deck detail."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "prep_get_deck",
        "description": (
            "Metadata for a single deck by name. Returns 404 if the user "
            "doesn't own a deck by that name."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "prep_list_cards",
        "description": (
            "Every card in a deck, with all fields (type, prompt, answer, "
            "choices, rubric, skeleton, language, regex, explanation). "
            "Same fields the CSV exporter emits."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "prep_export_deck_csv",
        "description": (
            "Render an entire deck as a CSV text body — the same format "
            "prep's /deck/<name>/export.csv route produces. Anki-friendly."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "prep_create_deck",
        "description": (
            "Create an empty SRS deck with the given name and optional "
            "context_prompt. Errors with 409 if a deck of that name "
            "already exists."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "2-30 lowercase chars / digits / hyphens.",
                },
                "context_prompt": {
                    "type": "string",
                    "description": "Free-form description used as AI context.",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "prep_import_csv",
        "description": (
            "Append CSV rows to a deck (creates the deck if it doesn't "
            "exist). Expects the same column shape prep_export_deck_csv "
            "emits. Returns inserted / skipped_duplicates / errors."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "csv": {
                    "type": "string",
                    "description": "Full CSV body with header row.",
                },
            },
            "required": ["name", "csv"],
        },
    },
]


# ---- JSON-RPC plumbing ---------------------------------------------------


def _jsonrpc_result(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _tool_error(message: str) -> dict:
    """MCP tool-level error envelope. NOT a JSON-RPC error — the call
    succeeded, but the tool itself reports a problem the agent should
    surface to the user."""
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }


def _tool_text(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": False}


# ---- tool implementations ------------------------------------------------


def _do_list_decks(user_id: str, _args: dict) -> dict:
    summaries = DeckRepo().list_summaries(user_id)
    payload = [
        {
            "name": s.name,
            "type": s.deck_type.value if s.deck_type else "srs",
            "card_count": s.total,
            "due": s.due,
            "pinned": s.pinned,
        }
        for s in summaries
    ]
    return _tool_text(_json(payload))


def _do_get_deck(user_id: str, args: dict) -> dict:
    name = (args.get("name") or "").strip()
    if not name:
        return _tool_error("missing required arg: name")
    repo = DeckRepo()
    deck_id = repo.find_id(user_id, name)
    if deck_id is None:
        return _tool_error(f"deck not found: {name!r}")
    meta = repo.get_meta(user_id, deck_id)
    deck_type = repo.get_type(user_id, deck_id)
    cards = QuestionRepo().list_in_deck(user_id, deck_id)
    return _tool_text(
        _json(
            {
                "name": name,
                "type": deck_type.value if deck_type else "srs",
                "context_prompt": meta.context_prompt,
                "card_count": len(cards),
            }
        )
    )


def _do_list_cards(user_id: str, args: dict) -> dict:
    name = (args.get("name") or "").strip()
    if not name:
        return _tool_error("missing required arg: name")
    deck_id = DeckRepo().find_id(user_id, name)
    if deck_id is None:
        return _tool_error(f"deck not found: {name!r}")
    from prep.decks.io import _questions_for_export

    questions = _questions_for_export(user_id, deck_id)
    payload = [
        {
            "type": q.type.value,
            "topic": q.topic,
            "prompt": q.prompt,
            "answer": q.answer,
            "choices": q.choices,
            "rubric": q.rubric,
            "skeleton": q.skeleton,
            "language": q.language,
            "answer_regex": q.answer_regex,
            "explanation": q.explanation,
        }
        for q in questions
    ]
    return _tool_text(_json({"deck": name, "cards": payload}))


def _do_export_csv(user_id: str, args: dict) -> dict:
    name = (args.get("name") or "").strip()
    if not name:
        return _tool_error("missing required arg: name")
    deck_id = DeckRepo().find_id(user_id, name)
    if deck_id is None:
        return _tool_error(f"deck not found: {name!r}")
    return _tool_text(deck_to_csv(user_id, deck_id))


def _do_create_deck(user_id: str, args: dict) -> dict:
    name = (args.get("name") or "").strip()
    if not name:
        return _tool_error("missing required arg: name")
    context = (args.get("context_prompt") or "").strip() or None
    repo = DeckRepo()
    if repo.find_id(user_id, name) is not None:
        return _tool_error(f"deck {name!r} already exists")
    deck_id = repo.create(user_id, name, context)
    return _tool_text(_json({"name": name, "id": deck_id}))


def _do_import_csv(user_id: str, args: dict) -> dict:
    name = (args.get("name") or "").strip()
    csv_text = args.get("csv") or ""
    if not name:
        return _tool_error("missing required arg: name")
    if not csv_text.strip():
        return _tool_error("missing required arg: csv (full CSV body)")
    outcome = csv_to_deck(
        user_id, name, csv_text, deck_repo=DeckRepo(), question_repo=QuestionRepo()
    )
    return _tool_text(_json(asdict(outcome)))


_TOOL_HANDLERS = {
    "prep_list_decks": _do_list_decks,
    "prep_get_deck": _do_get_deck,
    "prep_list_cards": _do_list_cards,
    "prep_export_deck_csv": _do_export_csv,
    "prep_create_deck": _do_create_deck,
    "prep_import_csv": _do_import_csv,
}


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, indent=2)


# ---- route ---------------------------------------------------------------


@router.post(
    "/mcp",
    tags=["MCP"],
    summary="MCP JSON-RPC endpoint",
    description=(
        "Single endpoint speaking JSON-RPC 2.0 over HTTP — the Model "
        "Context Protocol's streamable-HTTP transport. Supported methods: "
        "`initialize`, `tools/list`, `tools/call`, "
        "`notifications/initialized`. The tool catalog mirrors the REST "
        "API: list_decks, get_deck, list_cards, export_deck_csv, "
        "create_deck, import_csv."
    ),
)
async def mcp_endpoint(request: Request, user: dict = Depends(bearer_user)):
    """Handle one JSON-RPC 2.0 message. Auth is identical to the REST
    API — `Authorization: Bearer prep_pat_…`. The same user-scoped
    repos and DeckRepo IDOR guards apply, so cross-user data leaks
    can't sneak in through the MCP surface."""
    user_id = user["tailscale_login"]
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(_jsonrpc_error(None, -32700, "Parse error"), status_code=400)

    if not isinstance(body, dict):
        return JSONResponse(_jsonrpc_error(None, -32600, "Invalid Request"), status_code=400)

    req_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}

    if method == "initialize":
        return _jsonrpc_result(
            req_id,
            {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": _SERVER_NAME, "version": _SERVER_VERSION},
            },
        )

    if method == "notifications/initialized":
        # Client-sent notification; spec says no response (no `id` on
        # notifications). Return 204 to match the lifecycle.
        return JSONResponse(None, status_code=204)

    if method == "tools/list":
        return _jsonrpc_result(req_id, {"tools": _TOOLS})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = _TOOL_HANDLERS.get(name)
        if handler is None:
            return JSONResponse(
                _jsonrpc_error(req_id, -32602, f"unknown tool: {name!r}"),
                status_code=200,
            )
        try:
            result = handler(user_id, args)
        except Exception as e:  # noqa: BLE001
            logger.exception("mcp tool %s raised", name)
            return JSONResponse(_jsonrpc_result(req_id, _tool_error(f"tool error: {e}")))
        return _jsonrpc_result(req_id, result)

    return JSONResponse(
        _jsonrpc_error(req_id, -32601, f"unknown method: {method!r}"), status_code=200
    )
