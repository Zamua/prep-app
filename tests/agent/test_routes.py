"""Route tests for the agent context — post-SDK migration.

The /settings/agent endpoints used to HTTP to a separate agent-server
container. After the SDK migration, the OAuth token lives in a file
under prep-data, and connect/disconnect mutate that file directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def token_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point PREP_DATA_DIR at a per-test tmp dir so token_store reads
    + writes don't touch the real /data volume."""
    monkeypatch.setenv("PREP_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    return tmp_path


def test_settings_agent_view_renders(client: TestClient, initialized_db: str, token_dir: Path):
    """GET /settings/agent renders even with no agent configured."""
    r = client.get("/settings/agent")
    assert r.status_code == 200


def test_settings_agent_connect_rejects_missing_token(
    client: TestClient, initialized_db: str, token_dir: Path
):
    """Empty token → 400 before we even touch disk."""
    r = client.post("/settings/agent/connect", data={"token": "   "})
    assert r.status_code == 400


def test_settings_agent_connect_rejects_wrong_prefix(
    client: TestClient, initialized_db: str, token_dir: Path
):
    """Anything not starting with sk-ant-oat01- is rejected. Catches
    pasted API keys (sk-ant-api03-...) before they hit the SDK and
    fail with a less-clear error message."""
    r = client.post("/settings/agent/connect", data={"token": "sk-ant-api03-not-a-setup-token"})
    assert r.status_code == 400
    assert "sk-ant-oat01-" in r.text


def test_settings_agent_connect_writes_token_to_volume(
    client: TestClient, initialized_db: str, token_dir: Path
):
    """Happy path: well-formed token → written to PREP_DATA_DIR/claude-oauth-token,
    stamped into CLAUDE_CODE_OAUTH_TOKEN env var, status flips to logged_in."""
    import os

    token = "sk-ant-oat01-fake-test-token"
    r = client.post("/settings/agent/connect", data={"token": token})
    assert r.status_code == 200
    # Persisted on disk
    on_disk = (token_dir / "claude-oauth-token").read_text().strip()
    assert on_disk == token
    # And loaded into the running process env so the SDK adapter
    # picks it up without a restart.
    assert os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") == token


def test_settings_agent_disconnect_removes_token(
    client: TestClient, initialized_db: str, token_dir: Path
):
    """Disconnect deletes the file + clears the env var. Idempotent —
    calling on an already-disconnected instance still 200s."""
    import os

    # Plant a token first via the connect route.
    token = "sk-ant-oat01-fake-test-token"
    client.post("/settings/agent/connect", data={"token": token})
    assert (token_dir / "claude-oauth-token").exists()

    r = client.post("/settings/agent/disconnect")
    assert r.status_code == 200
    assert not (token_dir / "claude-oauth-token").exists()
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ

    # Idempotent — second call still 200, no exception.
    r2 = client.post("/settings/agent/disconnect")
    assert r2.status_code == 200
