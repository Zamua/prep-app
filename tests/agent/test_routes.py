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
    + writes don't touch the real /data volume.

    Also force-clears CLAUDE_CODE_OAUTH_TOKEN on teardown. The /connect
    route stamps `os.environ[...]` directly (production behavior), and
    monkeypatch can't auto-revert mutations it didn't make — so without
    this teardown the env var leaks into later tests, causing the SDK
    adapter to think it has a real token and try to talk to Anthropic
    (3+ min hang per test that triggers a run_prompt call)."""
    import os

    monkeypatch.setenv("PREP_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    yield tmp_path
    os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)


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


# ---- BYOK routes ---------------------------------------------------------


@pytest.fixture
def _byok_master(monkeypatch):
    """BYOK routes need PREP_KEY_ENCRYPTION_SECRET in env to encrypt
    the posted key. Deterministic test key — never used outside tests."""
    monkeypatch.setenv("PREP_KEY_ENCRYPTION_SECRET", "dd" * 32)


def test_byok_connect_rejects_wrong_prefix(
    client: TestClient, initialized_db: str, token_dir: Path, _byok_master
):
    """OAuth-prefix keys aren't API keys — surface the mismatch clearly
    rather than letting Anthropic reject the request later."""
    r = client.post(
        "/settings/agent/byok/anthropic-api/connect",
        data={"api_key": "sk-ant-oat01-not-an-api-key"},
    )
    assert r.status_code == 400


def test_byok_connect_rejects_missing(
    client: TestClient, initialized_db: str, token_dir: Path, _byok_master
):
    r = client.post("/settings/agent/byok/anthropic-api/connect", data={"api_key": "  "})
    assert r.status_code == 400


def test_byok_connect_stores_and_metadata_round_trips(
    client: TestClient, initialized_db: str, token_dir: Path, _byok_master
):
    """Happy path: POST a valid key → 200 + masked prefix visible in the
    rendered page. The actual ciphertext is encrypted at rest (see
    tests/byok/test_repo.py for the storage-level guarantees)."""
    r = client.post(
        "/settings/agent/byok/anthropic-api/connect",
        data={"api_key": "sk-ant-api03-abcdefghijklmnop"},
    )
    assert r.status_code == 200
    # The masked prefix renders in the page so the user can verify
    # they pasted the right key. Last 4 chars of the secret.
    assert "mnop" in r.text


def test_byok_disconnect_is_idempotent(
    client: TestClient, initialized_db: str, token_dir: Path, _byok_master
):
    r1 = client.post("/settings/agent/byok/anthropic-api/disconnect")
    assert r1.status_code == 200
    # Second call with no row → still 200.
    r2 = client.post("/settings/agent/byok/anthropic-api/disconnect")
    assert r2.status_code == 200


def test_byok_connect_503_when_master_key_missing(
    client: TestClient, initialized_db: str, token_dir: Path, monkeypatch
):
    """Deploy without PREP_KEY_ENCRYPTION_SECRET → BYOK feature is
    disabled with a clear operator-facing message rather than a
    confusing crypto error."""
    monkeypatch.delenv("PREP_KEY_ENCRYPTION_SECRET", raising=False)
    r = client.post(
        "/settings/agent/byok/anthropic-api/connect",
        data={"api_key": "sk-ant-api03-abcdefghijklmnop"},
    )
    assert r.status_code == 503
    assert "PREP_KEY_ENCRYPTION_SECRET" in r.text


def test_byok_route_404s_on_unknown_provider(
    client: TestClient, initialized_db: str, token_dir: Path, _byok_master
):
    """The {provider} URL slug is bound to the Provider enum; anything
    else returns 404 so an attacker can't probe which providers we
    support by getting different error shapes."""
    r = client.post(
        "/settings/agent/byok/not-a-real-provider/connect",
        data={"api_key": "sk-anything-xxxxxxxxxxxxxxxxxx"},
    )
    assert r.status_code == 404


def test_byok_connect_openai_happy_path(
    client: TestClient, initialized_db: str, token_dir: Path, _byok_master
):
    r = client.post(
        "/settings/agent/byok/openai-api/connect",
        data={"api_key": "sk-proj-abcdefghijklmnop"},
    )
    assert r.status_code == 200
    assert "mnop" in r.text  # masked suffix renders


def test_byok_connect_openrouter_happy_path(
    client: TestClient, initialized_db: str, token_dir: Path, _byok_master
):
    r = client.post(
        "/settings/agent/byok/openrouter-api/connect",
        data={"api_key": "sk-or-v1-abcdefghijklmnop"},
    )
    assert r.status_code == 200
    assert "mnop" in r.text


def test_byok_connect_rejects_wrong_prefix_per_provider(
    client: TestClient, initialized_db: str, token_dir: Path, _byok_master
):
    """A sk-ant key sent to the OpenAI slug must be rejected — even
    though `sk-` technically matches OpenAI's broad prefix list, the
    Anthropic shape gets caught first via provider_for_key. Here we
    check the route's prefix validator (which only consults the
    given provider's own prefixes) still rejects the obvious
    cross-provider mistake."""
    r = client.post(
        "/settings/agent/byok/anthropic-api/connect",
        data={"api_key": "sk-or-v1-routerkey"},  # wrong provider entirely
    )
    assert r.status_code == 400


def test_byok_use_marks_active_provider(
    client: TestClient, initialized_db: str, token_dir: Path, _byok_master
):
    """POST /byok/<provider>/use sets the user's active provider when
    they have a stored key for it."""
    from prep.auth.repo import UserRepo

    # Save an OpenAI key first.
    r = client.post(
        "/settings/agent/byok/openai-api/connect",
        data={"api_key": "sk-proj-zzzz1111aaaa2222"},
    )
    assert r.status_code == 200

    r2 = client.post("/settings/agent/byok/openai-api/use")
    assert r2.status_code == 200
    assert UserRepo().get_active_byok_provider("testuser@example.com") == "openai-api"


def test_byok_use_refuses_when_no_key_saved(
    client: TestClient, initialized_db: str, token_dir: Path, _byok_master
):
    """Defense against a stale form post — `/use` requires a stored
    key for the provider. UX-wise the button isn't even rendered
    when there's no key, so this only fires on a stale tab."""
    r = client.post("/settings/agent/byok/anthropic-api/use")
    assert r.status_code == 400


def test_byok_disconnect_clears_active_when_it_was_chosen(
    client: TestClient, initialized_db: str, token_dir: Path, _byok_master
):
    """If the user disconnects the provider they had marked active,
    the active_byok_provider column gets cleared so the selector
    falls back to its built-in precedence on the next call."""
    from prep.auth.repo import UserRepo

    client.post(
        "/settings/agent/byok/openrouter-api/connect",
        data={"api_key": "sk-or-v1-aaaabbbbccccdddd"},
    )
    client.post("/settings/agent/byok/openrouter-api/use")
    assert UserRepo().get_active_byok_provider("testuser@example.com") == "openrouter-api"

    client.post("/settings/agent/byok/openrouter-api/disconnect")
    assert UserRepo().get_active_byok_provider("testuser@example.com") is None
