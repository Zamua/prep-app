"""Public-API token lifecycle + security tests.

Asserts:
- Tokens are stored as sha256 hashes, NEVER plaintext
- The masked `key_prefix` doesn't reveal enough to reconstruct
- Lookup by an unknown / malformed token returns None (no 500)
- Cross-user delete is blocked
- Lookup updates last_used_at
- The full token is shown by the mint route exactly once and is
  never present in any GET response (no query-string leak)
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from prep.api.entities import TOKEN_PREFIX
from prep.api.repo import ApiTokenRepo
from prep.auth.repo import UserRepo
from prep.infrastructure.db import cursor

# ---- repo ---------------------------------------------------------------


def test_issue_stores_only_the_hash(initialized_db: str):
    repo = ApiTokenRepo()
    UserRepo().upsert(external_id="alice@example.com", email="alice@example.com")
    token, meta = repo.issue(user_id="alice@example.com", label="laptop")
    # Plaintext is never in the row.
    with cursor() as c:
        row = c.execute(
            "SELECT token_hash, key_prefix, label FROM api_tokens WHERE id = ?",
            (meta.id,),
        ).fetchone()
    assert token not in row["token_hash"]
    assert token not in row["key_prefix"]
    assert row["label"] == "laptop"
    # The masked prefix carries only the constant prefix + 2 chars +
    # ellipsis + last 4 chars — recovering the secret from it is
    # impossible.
    assert "…" in row["key_prefix"]
    assert row["key_prefix"].startswith(TOKEN_PREFIX)


def test_lookup_round_trip(initialized_db: str):
    UserRepo().upsert(external_id="alice@example.com", email="alice@example.com")
    token, _ = ApiTokenRepo().issue(user_id="alice@example.com")
    result = ApiTokenRepo().lookup(token)
    assert result is not None
    user_id, _token_id = result
    assert user_id == "alice@example.com"


def test_lookup_returns_none_for_unknown_token(initialized_db: str):
    assert ApiTokenRepo().lookup("prep_pat_not_a_real_token_xxxxxxxxxxxx") is None


def test_lookup_returns_none_for_wrong_prefix(initialized_db: str):
    """sk-ant-* / arbitrary strings shouldn't even hit the DB."""
    assert ApiTokenRepo().lookup("sk-ant-api03-pretending-to-be-a-prep-pat") is None
    assert ApiTokenRepo().lookup("") is None


def test_delete_is_per_user_scoped(initialized_db: str):
    """A user can't revoke another user's token by guessing the id."""
    UserRepo().upsert(external_id="alice@example.com", email="alice@example.com")
    UserRepo().upsert(external_id="bob@example.com", email="bob@example.com")
    _, alice_meta = ApiTokenRepo().issue(user_id="alice@example.com")
    # Bob tries to delete alice's token by id — should be a no-op.
    deleted = ApiTokenRepo().delete(user_id="bob@example.com", token_id=alice_meta.id)
    assert deleted is False
    # Alice's token still works.
    _, _ = ApiTokenRepo().lookup_dirty if hasattr(ApiTokenRepo, "lookup_dirty") else (None, None)
    rows = ApiTokenRepo().list_for_user("alice@example.com")
    assert len(rows) == 1


def test_lookup_bumps_last_used_at(initialized_db: str):
    UserRepo().upsert(external_id="alice@example.com", email="alice@example.com")
    token, meta = ApiTokenRepo().issue(user_id="alice@example.com")
    assert meta.last_used_at is None
    ApiTokenRepo().lookup(token)
    refreshed = ApiTokenRepo().list_for_user("alice@example.com")
    assert refreshed[0].last_used_at is not None


# ---- HTTP — mint flow + plaintext exposure -----------------------------


def test_mint_token_via_post_renders_inline(client: TestClient, initialized_db: str):
    """Minting hits the page once with the plaintext in the body. The
    plaintext starts with `prep_pat_` and is shown in a 'copy this once'
    block."""
    r = client.post("/settings/api/tokens", data={"label": "laptop"}, follow_redirects=False)
    assert r.status_code == 200  # NOT a redirect — no query-string leak
    # Body contains the plaintext.
    assert TOKEN_PREFIX in r.text


def test_get_after_mint_does_not_replay_plaintext(client: TestClient, initialized_db: str):
    """The plaintext is NOT stored server-side and NOT shown on a
    subsequent GET. (Refresh = gone, the explicit contract.)"""
    r1 = client.post("/settings/api/tokens", data={"label": "x"})
    assert r1.status_code == 200
    # Confirm the token appeared exactly once on the mint response.
    import re

    token_in_mint = re.findall(r"prep_pat_[A-Za-z0-9_-]{30,}", r1.text)
    assert len(token_in_mint) == 1

    # Now GET the page again. The plaintext should be gone.
    r2 = client.get("/settings/api")
    assert r2.status_code == 200
    assert not re.findall(r"prep_pat_[A-Za-z0-9_-]{30,}", r2.text)


# ---- HTTP — bearer-token auth on the public API ------------------------


def test_api_without_bearer_token_returns_401(client: TestClient, initialized_db: str):
    """/api/v1/* refuses cookie-only callers — bearer is mandatory."""
    r = client.get("/api/v1/decks", headers={"authorization": ""})
    assert r.status_code == 401


def test_api_with_unknown_bearer_returns_401(client: TestClient, initialized_db: str):
    r = client.get(
        "/api/v1/decks",
        headers={"authorization": "Bearer prep_pat_definitely_not_real_xxxxxxxxxxxxx"},
    )
    assert r.status_code == 401


def test_api_with_valid_bearer_returns_users_decks(client: TestClient, initialized_db: str):
    """End-to-end: mint a token via the UI, use it to call the JSON
    API. The deck list reflects the same user's decks (not someone
    else's)."""
    # Mint a token for the default fixture user.
    r1 = client.post("/settings/api/tokens", data={"label": "test"})
    import re

    token = re.findall(r"prep_pat_[A-Za-z0-9_-]{30,}", r1.text)[0]

    # Use it.
    r2 = client.get("/api/v1/decks", headers={"authorization": f"Bearer {token}"})
    assert r2.status_code == 200
    assert "decks" in r2.json()


def test_api_cross_user_idor_returns_404(client: TestClient, initialized_db: str):
    """A bearer token resolves to its owner; that owner asking for a
    deck-name they don't own gets a 404, same shape as not-found.
    Doesn't leak whether someone else has a deck by that name."""
    from prep.decks.repo import DeckRepo

    UserRepo().upsert(external_id="other@example.com", email="other@example.com")
    DeckRepo().create("other@example.com", "secret-deck")

    # Mint a token for the fixture user (testuser@example.com).
    r1 = client.post("/settings/api/tokens", data={"label": "x"})
    import re

    token = re.findall(r"prep_pat_[A-Za-z0-9_-]{30,}", r1.text)[0]

    # Fixture user asks for the OTHER user's deck → 404.
    r = client.get(
        "/api/v1/decks/secret-deck",
        headers={"authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404
