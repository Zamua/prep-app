"""MCP-over-HTTP smoke tests + llms.txt."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient


def _mint_token(client: TestClient) -> str:
    r = client.post("/settings/api/tokens", data={"label": "mcp-tests"})
    assert r.status_code == 200
    return re.findall(r"prep_pat_[A-Za-z0-9_-]{30,}", r.text)[0]


def _rpc(client: TestClient, token: str, method: str, params: dict | None = None, req_id: int = 1):
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post(
        "/mcp",
        json=body,
        headers={"authorization": f"Bearer {token}"},
    )


# ---- auth boundary -------------------------------------------------------


def test_mcp_requires_bearer_token(client: TestClient, initialized_db: str):
    """No bearer → 401, no JSON-RPC frame even attempted."""
    r = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert r.status_code == 401


def test_mcp_rejects_unknown_bearer(client: TestClient, initialized_db: str):
    r = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"authorization": "Bearer prep_pat_definitely_not_real_xxxxxxxxxxxxx"},
    )
    assert r.status_code == 401


# ---- protocol surface ----------------------------------------------------


def test_mcp_initialize_handshake(client: TestClient, initialized_db: str):
    token = _mint_token(client)
    r = _rpc(client, token, "initialize", {"protocolVersion": "2025-06-18"})
    assert r.status_code == 200
    payload = r.json()
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == 1
    assert "result" in payload
    info = payload["result"]
    assert info["protocolVersion"]
    assert info["serverInfo"]["name"] == "prep"
    assert "tools" in info["capabilities"]


def test_mcp_tools_list_advertises_prep_tools(client: TestClient, initialized_db: str):
    token = _mint_token(client)
    r = _rpc(client, token, "tools/list")
    assert r.status_code == 200
    tools = {t["name"] for t in r.json()["result"]["tools"]}
    assert "prep_list_decks" in tools
    assert "prep_export_deck_csv" in tools
    assert "prep_import_csv" in tools


# ---- tool dispatch -------------------------------------------------------


def test_mcp_tools_call_lists_decks(client: TestClient, initialized_db: str):
    """End-to-end: create a deck via the REST API, list it via MCP."""
    token = _mint_token(client)
    # Make a deck so list isn't empty.
    client.post(
        "/api/v1/decks",
        json={"name": "via-mcp-test"},
        headers={"authorization": f"Bearer {token}"},
    )
    r = _rpc(
        client,
        token,
        "tools/call",
        {"name": "prep_list_decks", "arguments": {}},
    )
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["isError"] is False, result["content"][0]["text"]
    text = result["content"][0]["text"]
    assert "via-mcp-test" in text


def test_mcp_tools_call_unknown_tool_returns_rpc_error(client: TestClient, initialized_db: str):
    token = _mint_token(client)
    r = _rpc(
        client,
        token,
        "tools/call",
        {"name": "prep_nuke_everything", "arguments": {}},
    )
    assert r.status_code == 200
    assert "error" in r.json()


def test_mcp_tools_call_idor_safe(client: TestClient, initialized_db: str):
    """Asking for someone else's deck via MCP gets the same isError
    shape as not-found — no leak of whether the deck exists for
    another user."""
    from prep.auth.repo import UserRepo
    from prep.decks.repo import DeckRepo

    UserRepo().upsert(external_id="other@example.com", email="other@example.com")
    DeckRepo().create("other@example.com", "their-deck")

    token = _mint_token(client)
    r = _rpc(
        client,
        token,
        "tools/call",
        {"name": "prep_get_deck", "arguments": {"name": "their-deck"}},
    )
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["isError"] is True
    assert "deck not found" in result["content"][0]["text"]


# ---- llms.txt ------------------------------------------------------------


def test_llms_txt_describes_mcp(client: TestClient, initialized_db: str, monkeypatch):
    monkeypatch.setenv("PREP_AUTH_MODE", "clerk")
    r = client.get("/llms.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    body = r.text
    # Mentions the MCP endpoint + the REST API + the auth model.
    assert "/mcp" in body
    assert "/api/v1" in body
    assert "Authorization: Bearer prep_pat_" in body
    # Lists at least a couple of the tools.
    assert "prep_list_decks" in body
    assert "prep_import_csv" in body


# ---- expanded tool surface (rename / delete / pin / topic / card CRUD / apkg) ----


def test_mcp_rename_deck(client: TestClient, initialized_db: str):
    from prep.decks.repo import DeckRepo

    token = _mint_token(client)
    DeckRepo().create(initialized_db, "old-name")
    r = _rpc(
        client,
        token,
        "tools/call",
        {"name": "prep_rename_deck", "arguments": {"name": "old-name", "new_name": "new-name"}},
    )
    body = r.json()["result"]
    assert body["isError"] is False, body["content"][0]["text"]
    assert DeckRepo().find_id(initialized_db, "old-name") is None
    assert DeckRepo().find_id(initialized_db, "new-name") is not None


def test_mcp_delete_deck(client: TestClient, initialized_db: str):
    from prep.decks.repo import DeckRepo

    token = _mint_token(client)
    DeckRepo().create(initialized_db, "doomed")
    r = _rpc(
        client, token, "tools/call", {"name": "prep_delete_deck", "arguments": {"name": "doomed"}}
    )
    assert r.json()["result"]["isError"] is False
    assert DeckRepo().find_id(initialized_db, "doomed") is None


def test_mcp_set_pinned(client: TestClient, initialized_db: str):
    from prep.decks.repo import DeckRepo

    token = _mint_token(client)
    DeckRepo().create(initialized_db, "pin-me")
    r = _rpc(
        client,
        token,
        "tools/call",
        {"name": "prep_set_deck_pinned", "arguments": {"name": "pin-me", "pinned": True}},
    )
    assert r.json()["result"]["isError"] is False
    summaries = DeckRepo().list_summaries(initialized_db)
    pinned = next(s for s in summaries if s.name == "pin-me")
    assert pinned.pinned is True


def test_mcp_card_crud(client: TestClient, initialized_db: str):
    from prep.decks.repo import DeckRepo, QuestionRepo

    token = _mint_token(client)
    DeckRepo().create(initialized_db, "crud-test")

    # Add
    r = _rpc(
        client,
        token,
        "tools/call",
        {
            "name": "prep_add_card",
            "arguments": {
                "deck": "crud-test",
                "type": "short",
                "prompt": "Capital of France?",
                "answer": "Paris",
            },
        },
    )
    out = r.json()["result"]
    assert out["isError"] is False, out["content"][0]["text"]
    import json as _json

    payload = _json.loads(out["content"][0]["text"])
    qid = payload["id"]

    # Get
    r2 = _rpc(client, token, "tools/call", {"name": "prep_get_card", "arguments": {"card_id": qid}})
    got = _json.loads(r2.json()["result"]["content"][0]["text"])
    assert got["prompt"] == "Capital of France?"
    assert got["answer"] == "Paris"

    # Update
    r3 = _rpc(
        client,
        token,
        "tools/call",
        {
            "name": "prep_update_card",
            "arguments": {
                "card_id": qid,
                "type": "short",
                "prompt": "Capital of Spain?",
                "answer": "Madrid",
            },
        },
    )
    assert r3.json()["result"]["isError"] is False
    updated = QuestionRepo().get(initialized_db, qid)
    assert updated.answer == "Madrid"

    # Suspend
    r4 = _rpc(
        client,
        token,
        "tools/call",
        {
            "name": "prep_suspend_card",
            "arguments": {"card_id": qid, "suspended": True},
        },
    )
    assert r4.json()["result"]["isError"] is False

    # Delete
    r5 = _rpc(
        client, token, "tools/call", {"name": "prep_delete_card", "arguments": {"card_id": qid}}
    )
    assert r5.json()["result"]["isError"] is False
    assert QuestionRepo().get(initialized_db, qid) is None


def test_mcp_apkg_round_trip(client: TestClient, initialized_db: str):
    """Export a deck to .apkg base64, then import back as a fresh deck."""
    import base64 as _b64
    import json as _json

    from prep.decks.entities import NewQuestion, QuestionType
    from prep.decks.repo import DeckRepo, QuestionRepo

    deck_id = DeckRepo().get_or_create(initialized_db, "src-deck")
    QuestionRepo().add(
        initialized_db, deck_id, NewQuestion(type=QuestionType.SHORT, prompt="Q", answer="A")
    )

    token = _mint_token(client)
    r = _rpc(
        client,
        token,
        "tools/call",
        {"name": "prep_export_deck_apkg", "arguments": {"name": "src-deck"}},
    )
    payload = _json.loads(r.json()["result"]["content"][0]["text"])
    assert payload["filename"] == "src-deck.apkg"
    assert payload["byte_count"] > 100
    blob_b64 = payload["apkg_base64"]
    # Sanity: actually decodes
    _b64.b64decode(blob_b64, validate=True)

    # Re-import as a new deck.
    r2 = _rpc(
        client,
        token,
        "tools/call",
        {
            "name": "prep_import_apkg",
            "arguments": {"name": "dst-deck", "apkg_base64": blob_b64},
        },
    )
    out = _json.loads(r2.json()["result"]["content"][0]["text"])
    assert out["inserted"] >= 1
    dst_id = DeckRepo().find_id(initialized_db, "dst-deck")
    cards = QuestionRepo().list_in_deck(initialized_db, dst_id)
    assert any(c.prompt == "Q" for c in cards)


def test_mcp_tools_list_includes_all_17(client: TestClient, initialized_db: str):
    token = _mint_token(client)
    r = _rpc(client, token, "tools/list")
    names = {t["name"] for t in r.json()["result"]["tools"]}
    expected = {
        "prep_list_decks",
        "prep_get_deck",
        "prep_create_deck",
        "prep_rename_deck",
        "prep_delete_deck",
        "prep_set_deck_pinned",
        "prep_set_topic_prompt",
        "prep_list_cards",
        "prep_get_card",
        "prep_add_card",
        "prep_update_card",
        "prep_delete_card",
        "prep_suspend_card",
        "prep_export_deck_csv",
        "prep_import_csv",
        "prep_export_deck_apkg",
        "prep_import_apkg",
    }
    missing = expected - names
    assert not missing, f"missing tools: {missing}"


def test_mcp_card_crud_idor_safe(client: TestClient, initialized_db: str):
    """A card owned by another user can't be read / updated / deleted
    via its raw id."""
    from prep.auth.repo import UserRepo
    from prep.decks.entities import NewQuestion, QuestionType
    from prep.decks.repo import DeckRepo, QuestionRepo

    UserRepo().upsert(external_id="other@example.com", email="other@example.com")
    deck_id = DeckRepo().get_or_create("other@example.com", "their-deck")
    qid = QuestionRepo().add(
        "other@example.com",
        deck_id,
        NewQuestion(type=QuestionType.SHORT, prompt="theirs", answer="theirs"),
    )

    token = _mint_token(client)
    for tool, args in [
        ("prep_get_card", {"card_id": qid}),
        ("prep_delete_card", {"card_id": qid}),
        ("prep_suspend_card", {"card_id": qid, "suspended": True}),
    ]:
        r = _rpc(client, token, "tools/call", {"name": tool, "arguments": args})
        body = r.json()["result"]
        assert body["isError"] is True, f"{tool} should have failed (IDOR)"
        assert "not found" in body["content"][0]["text"].lower()
