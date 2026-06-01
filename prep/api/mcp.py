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

Decks:
- `prep_list_decks` — list the caller's decks
- `prep_get_deck` — deck metadata + card count
- `prep_create_deck` — make a new deck
- `prep_rename_deck` — rename a deck
- `prep_delete_deck` — delete a deck (cascades cards + reviews)
- `prep_set_deck_pinned` — pin / unpin
- `prep_set_topic_prompt` — update the AI-context prompt

Cards:
- `prep_list_cards` — every card in a deck
- `prep_get_card` — single card by id
- `prep_add_card` — add a card to a deck
- `prep_update_card` — edit a card's fields
- `prep_delete_card` — delete a card
- `prep_suspend_card` — suspend / unsuspend

Imports / exports:
- `prep_export_deck_csv` — deck as CSV text
- `prep_import_csv` — append CSV rows to a deck
- `prep_export_deck_apkg` — deck as Anki .apkg (base64-encoded bytes)
- `prep_import_apkg` — import a .apkg (base64-encoded bytes)

CSV + JSON shapes share `prep.decks.io` with the REST API so the
two surfaces stay byte-identical.
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
    {
        "name": "prep_rename_deck",
        "description": (
            "Rename a deck. The new name must be unused (otherwise 409). "
            "All cards / reviews / sessions follow the rename via the "
            "FK chain — no data migration."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Current deck name."},
                "new_name": {
                    "type": "string",
                    "description": "Target name. 2-30 lowercase chars / digits / hyphens.",
                },
            },
            "required": ["name", "new_name"],
        },
    },
    {
        "name": "prep_delete_deck",
        "description": (
            "Delete a deck and (via FK CASCADE) all its questions, cards, "
            "reviews, and study sessions. Irreversible — confirm before "
            "calling."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "prep_set_deck_pinned",
        "description": (
            "Pin or unpin a deck on the user's index. Pinned decks float "
            "to the top of the library list."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "pinned": {"type": "boolean"},
            },
            "required": ["name", "pinned"],
        },
    },
    {
        "name": "prep_set_topic_prompt",
        "description": (
            "Set the AI-context prompt for a deck — used when prep "
            "generates new cards for this deck. Pass an empty string "
            "to clear."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "context_prompt": {"type": "string"},
            },
            "required": ["name", "context_prompt"],
        },
    },
    {
        "name": "prep_get_card",
        "description": "Fetch a single card by its numeric id.",
        "inputSchema": {
            "type": "object",
            "properties": {"card_id": {"type": "integer"}},
            "required": ["card_id"],
        },
    },
    {
        "name": "prep_add_card",
        "description": (
            "Add a single card to a deck. Type must be one of "
            "short | mcq | multi | code. Required fields by type: short "
            "+ code need prompt + answer; mcq + multi additionally need "
            "choices (array of strings). code optionally takes language "
            "+ skeleton."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "deck": {"type": "string", "description": "Deck name."},
                "type": {
                    "type": "string",
                    "enum": ["short", "mcq", "multi", "code"],
                },
                "prompt": {"type": "string"},
                "answer": {
                    "type": "string",
                    "description": (
                        "For mcq: the correct value. For multi: JSON-encoded "
                        "array of correct values. For short / code: the canonical "
                        "answer."
                    ),
                },
                "topic": {"type": "string"},
                "choices": {"type": "array", "items": {"type": "string"}},
                "rubric": {"type": "string"},
                "skeleton": {"type": "string"},
                "language": {"type": "string"},
                "answer_regex": {"type": "string"},
                "explanation": {"type": "string"},
            },
            "required": ["deck", "type", "prompt", "answer"],
        },
    },
    {
        "name": "prep_update_card",
        "description": (
            "Replace a card's editable fields. Pass every field you want "
            "to keep — the existing values are NOT merged. Pull current "
            "values via prep_get_card first if you want a partial edit."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "card_id": {"type": "integer"},
                "type": {
                    "type": "string",
                    "enum": ["short", "mcq", "multi", "code"],
                },
                "prompt": {"type": "string"},
                "answer": {"type": "string"},
                "topic": {"type": "string"},
                "choices": {"type": "array", "items": {"type": "string"}},
                "rubric": {"type": "string"},
                "skeleton": {"type": "string"},
                "language": {"type": "string"},
                "answer_regex": {"type": "string"},
                "explanation": {"type": "string"},
            },
            "required": ["card_id", "type", "prompt", "answer"],
        },
    },
    {
        "name": "prep_delete_card",
        "description": (
            "Delete a card. Cascade drops the SRS row + every review for "
            "this card. Irreversible."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"card_id": {"type": "integer"}},
            "required": ["card_id"],
        },
    },
    {
        "name": "prep_suspend_card",
        "description": (
            "Suspend (hide from study sessions) or un-suspend a card. "
            "The card keeps its SRS state; suspension just removes it "
            "from the due queue."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "card_id": {"type": "integer"},
                "suspended": {"type": "boolean"},
            },
            "required": ["card_id", "suspended"],
        },
    },
    {
        "name": "prep_export_deck_apkg",
        "description": (
            "Render a deck as an Anki .apkg file. Returns a base64-encoded "
            "binary; the client can write it to disk and import into Anki, "
            "AnkiDroid, or any other consumer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "prep_import_apkg",
        "description": (
            "Import an Anki .apkg file into a deck. Pass the .apkg bytes "
            "as a base64-encoded string. Creates the deck if missing. "
            "Returns inserted / skipped_duplicates / cloze_skipped / errors."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "apkg_base64": {
                    "type": "string",
                    "description": "Raw .apkg bytes, base64-encoded.",
                },
            },
            "required": ["name", "apkg_base64"],
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


# ---- Deck management -----------------------------------------------------


def _do_rename_deck(user_id: str, args: dict) -> dict:
    name = (args.get("name") or "").strip()
    new_name = (args.get("new_name") or "").strip()
    if not name or not new_name:
        return _tool_error("missing required args: name, new_name")
    repo = DeckRepo()
    if repo.find_id(user_id, name) is None:
        return _tool_error(f"deck not found: {name!r}")
    if repo.find_id(user_id, new_name) is not None:
        return _tool_error(f"deck {new_name!r} already exists")
    ok = repo.rename(user_id, name, new_name)
    if not ok:
        return _tool_error("rename failed")
    return _tool_text(_json({"name": new_name}))


def _do_delete_deck(user_id: str, args: dict) -> dict:
    name = (args.get("name") or "").strip()
    if not name:
        return _tool_error("missing required arg: name")
    repo = DeckRepo()
    if repo.find_id(user_id, name) is None:
        return _tool_error(f"deck not found: {name!r}")
    repo.delete(user_id, name)
    return _tool_text(_json({"ok": True, "deleted": name}))


def _do_set_deck_pinned(user_id: str, args: dict) -> dict:
    name = (args.get("name") or "").strip()
    if not name:
        return _tool_error("missing required arg: name")
    pinned = args.get("pinned")
    if not isinstance(pinned, bool):
        return _tool_error("pinned must be a boolean")
    repo = DeckRepo()
    deck_id = repo.find_id(user_id, name)
    if deck_id is None:
        return _tool_error(f"deck not found: {name!r}")
    repo.set_pinned(user_id, deck_id, pinned)
    return _tool_text(_json({"name": name, "pinned": pinned}))


def _do_set_topic_prompt(user_id: str, args: dict) -> dict:
    name = (args.get("name") or "").strip()
    if not name:
        return _tool_error("missing required arg: name")
    context = args.get("context_prompt") or ""
    repo = DeckRepo()
    if repo.find_id(user_id, name) is None:
        return _tool_error(f"deck not found: {name!r}")
    repo.update_context_prompt(user_id, name, context)
    return _tool_text(_json({"name": name, "context_prompt": context}))


# ---- Card CRUD ----------------------------------------------------------


def _card_to_dict(q) -> dict:
    return {
        "id": q.id,
        "deck_id": q.deck_id,
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
        "suspended": getattr(q, "suspended", False),
    }


def _build_new_question(args: dict):
    """Shared validation/builder for prep_add_card + prep_update_card.
    Returns either a NewQuestion entity or a `_tool_error` dict."""
    from prep.decks.entities import NewQuestion, QuestionType

    qtype_raw = (args.get("type") or "").strip().lower()
    try:
        qtype = QuestionType(qtype_raw)
    except ValueError:
        return _tool_error(f"unknown type {qtype_raw!r}; expected short|mcq|multi|code")
    prompt = (args.get("prompt") or "").strip()
    answer = (args.get("answer") or "").strip()
    if not prompt:
        return _tool_error("missing required arg: prompt")
    if not answer:
        return _tool_error("missing required arg: answer")
    choices = args.get("choices")
    if choices is not None and not isinstance(choices, list):
        return _tool_error("choices must be an array of strings")
    try:
        return NewQuestion(
            type=qtype,
            topic=(args.get("topic") or "").strip() or None,
            prompt=prompt,
            answer=answer,
            choices=[str(c) for c in choices] if choices else None,
            rubric=(args.get("rubric") or "").strip() or None,
            skeleton=(args.get("skeleton") or "").strip() or None,
            language=(args.get("language") or "").strip() or None,
            answer_regex=(args.get("answer_regex") or "").strip() or None,
            explanation=(args.get("explanation") or "").strip() or None,
        )
    except Exception as e:  # noqa: BLE001 — pydantic surface
        return _tool_error(f"validation failed: {e}")


def _do_get_card(user_id: str, args: dict) -> dict:
    card_id = args.get("card_id")
    if not isinstance(card_id, int):
        return _tool_error("card_id must be an integer")
    q = QuestionRepo().get(user_id, card_id)
    if q is None:
        return _tool_error(f"card not found: {card_id}")
    return _tool_text(_json(_card_to_dict(q)))


def _do_add_card(user_id: str, args: dict) -> dict:
    deck_name = (args.get("deck") or "").strip()
    if not deck_name:
        return _tool_error("missing required arg: deck")
    deck_id = DeckRepo().find_id(user_id, deck_name)
    if deck_id is None:
        return _tool_error(f"deck not found: {deck_name!r}")
    built = _build_new_question(args)
    if isinstance(built, dict):  # error envelope
        return built
    qid = QuestionRepo().add(user_id, deck_id, built)
    return _tool_text(_json({"id": qid}))


def _do_update_card(user_id: str, args: dict) -> dict:
    card_id = args.get("card_id")
    if not isinstance(card_id, int):
        return _tool_error("card_id must be an integer")
    if QuestionRepo().get(user_id, card_id) is None:
        return _tool_error(f"card not found: {card_id}")
    built = _build_new_question(args)
    if isinstance(built, dict):
        return built
    QuestionRepo().update(user_id, card_id, built)
    return _tool_text(_json({"id": card_id}))


def _do_delete_card(user_id: str, args: dict) -> dict:
    card_id = args.get("card_id")
    if not isinstance(card_id, int):
        return _tool_error("card_id must be an integer")
    ok = QuestionRepo().delete(user_id, card_id)
    if not ok:
        return _tool_error(f"card not found: {card_id}")
    return _tool_text(_json({"ok": True, "deleted_id": card_id}))


def _do_suspend_card(user_id: str, args: dict) -> dict:
    card_id = args.get("card_id")
    suspended = args.get("suspended")
    if not isinstance(card_id, int):
        return _tool_error("card_id must be an integer")
    if not isinstance(suspended, bool):
        return _tool_error("suspended must be a boolean")
    if QuestionRepo().get(user_id, card_id) is None:
        return _tool_error(f"card not found: {card_id}")
    QuestionRepo().set_suspended(user_id, card_id, suspended)
    return _tool_text(_json({"id": card_id, "suspended": suspended}))


# ---- .apkg in/out -------------------------------------------------------


def _do_export_apkg(user_id: str, args: dict) -> dict:
    import base64

    from prep.decks.anki_export import deck_to_apkg

    name = (args.get("name") or "").strip()
    if not name:
        return _tool_error("missing required arg: name")
    deck_id = DeckRepo().find_id(user_id, name)
    if deck_id is None:
        return _tool_error(f"deck not found: {name!r}")
    blob = deck_to_apkg(user_id, deck_id, name)
    return _tool_text(
        _json(
            {
                "filename": f"{name}.apkg",
                "apkg_base64": base64.b64encode(blob).decode("ascii"),
                "byte_count": len(blob),
            }
        )
    )


def _do_import_apkg(user_id: str, args: dict) -> dict:
    import base64

    from prep.decks.anki import apkg_to_deck

    name = (args.get("name") or "").strip()
    if not name:
        return _tool_error("missing required arg: name")
    b64 = args.get("apkg_base64") or ""
    if not b64:
        return _tool_error("missing required arg: apkg_base64")
    try:
        blob = base64.b64decode(b64, validate=True)
    except Exception as e:  # noqa: BLE001
        return _tool_error(f"apkg_base64 didn't decode: {e}")
    try:
        outcome = apkg_to_deck(
            user_id, name, blob, deck_repo=DeckRepo(), question_repo=QuestionRepo()
        )
    except ValueError as e:
        return _tool_error(str(e))
    return _tool_text(_json(asdict(outcome)))


_TOOL_HANDLERS = {
    "prep_list_decks": _do_list_decks,
    "prep_get_deck": _do_get_deck,
    "prep_list_cards": _do_list_cards,
    "prep_export_deck_csv": _do_export_csv,
    "prep_create_deck": _do_create_deck,
    "prep_import_csv": _do_import_csv,
    "prep_rename_deck": _do_rename_deck,
    "prep_delete_deck": _do_delete_deck,
    "prep_set_deck_pinned": _do_set_deck_pinned,
    "prep_set_topic_prompt": _do_set_topic_prompt,
    "prep_get_card": _do_get_card,
    "prep_add_card": _do_add_card,
    "prep_update_card": _do_update_card,
    "prep_delete_card": _do_delete_card,
    "prep_suspend_card": _do_suspend_card,
    "prep_export_deck_apkg": _do_export_apkg,
    "prep_import_apkg": _do_import_apkg,
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
