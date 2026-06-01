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
