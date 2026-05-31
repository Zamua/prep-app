"""BYOKRepo tests against a real sqlite via the standard
initialized_db / cursor fixtures. Asserts:
  - encrypt-on-write: secret never lands in DB as plaintext
  - decrypt-on-read: round-trip returns the original
  - upsert semantics on (user_id, provider)
  - delete + idempotent delete
  - metadata view never reveals the secret
"""

from __future__ import annotations

import pytest

from prep.auth.repo import UserRepo
from prep.byok import crypto
from prep.byok.entities import Provider
from prep.byok.repo import BYOKRepo
from prep.infrastructure.db import cursor

_KEY = bytes.fromhex("aa" * 32)


@pytest.fixture
def repo(initialized_db) -> BYOKRepo:
    # Inject a deterministic key so the test is hermetic — no env
    # mutation needed, no shared global state.
    return BYOKRepo(master_key=_KEY)


@pytest.fixture
def user_id(initialized_db) -> str:
    """The shared conftest seeds a user via the Tailscale default-user
    bypass; pick that one up by reading directly so tests stay
    decoupled from the auth-layer fixture details."""
    UserRepo().upsert(external_id="byok-tester@example.com", email="byok-tester@example.com")
    return "byok-tester@example.com"


def test_store_writes_ciphertext_not_plaintext(repo: BYOKRepo, user_id: str):
    repo.store(
        user_id=user_id,
        provider=Provider.ANTHROPIC_API,
        secret="sk-ant-api03-abcdefghijklmnop",
    )
    with cursor() as c:
        row = c.execute(
            "SELECT ciphertext, key_prefix FROM byok_credentials WHERE user_id=?",
            (user_id,),
        ).fetchone()
    # The plaintext is nowhere in the DB row.
    assert "sk-ant-api03-abcdefghijklmnop" not in row["ciphertext"]
    # And the prefix is the masked display form (safe to surface).
    assert row["key_prefix"].startswith("sk-ant-api03-")
    assert row["key_prefix"].endswith("mnop")
    assert "…" in row["key_prefix"]


def test_get_secret_roundtrips_through_decrypt(repo: BYOKRepo, user_id: str):
    plain = "sk-ant-api03-zzzz1111aaaa2222"
    repo.store(user_id=user_id, provider=Provider.ANTHROPIC_API, secret=plain)
    assert repo.get_secret(user_id=user_id, provider=Provider.ANTHROPIC_API) == plain


def test_get_secret_returns_none_for_missing(repo: BYOKRepo, user_id: str):
    assert repo.get_secret(user_id=user_id, provider=Provider.ANTHROPIC_API) is None


def test_get_secret_returns_none_for_different_user(repo: BYOKRepo, user_id: str):
    UserRepo().upsert(external_id="other@example.com", email="other@example.com")
    repo.store(user_id=user_id, provider=Provider.ANTHROPIC_API, secret="sk-ant-api03-mine")
    # Cross-user IDOR: the other user gets None, not a leak.
    assert repo.get_secret(user_id="other@example.com", provider=Provider.ANTHROPIC_API) is None


def test_store_is_upsert_on_user_provider(repo: BYOKRepo, user_id: str):
    repo.store(user_id=user_id, provider=Provider.ANTHROPIC_API, secret="sk-ant-api03-first")
    repo.store(user_id=user_id, provider=Provider.ANTHROPIC_API, secret="sk-ant-api03-replaced")
    # Only one row (PK enforces uniqueness, ON CONFLICT updates it).
    with cursor() as c:
        rows = c.execute("SELECT * FROM byok_credentials WHERE user_id=?", (user_id,)).fetchall()
    assert len(rows) == 1
    # And the latest secret round-trips.
    assert (
        repo.get_secret(user_id=user_id, provider=Provider.ANTHROPIC_API) == "sk-ant-api03-replaced"
    )


def test_delete_removes_row_and_is_idempotent(repo: BYOKRepo, user_id: str):
    repo.store(user_id=user_id, provider=Provider.ANTHROPIC_API, secret="sk-ant-api03-x")
    assert repo.delete(user_id=user_id, provider=Provider.ANTHROPIC_API) is True
    # Already gone — second delete is False but doesn't raise.
    assert repo.delete(user_id=user_id, provider=Provider.ANTHROPIC_API) is False
    assert repo.get_secret(user_id=user_id, provider=Provider.ANTHROPIC_API) is None


def test_metadata_view_carries_prefix_and_timestamps(repo: BYOKRepo, user_id: str):
    repo.store(
        user_id=user_id,
        provider=Provider.ANTHROPIC_API,
        secret="sk-ant-api03-abcdefghijklmnopqr",
    )
    meta = repo.metadata(user_id=user_id, provider=Provider.ANTHROPIC_API)
    assert meta is not None
    assert meta.provider is Provider.ANTHROPIC_API
    assert meta.key_prefix.startswith("sk-ant-api03-")
    assert meta.created_at  # ISO timestamp string
    assert meta.last_used_at is None


def test_touch_last_used_updates_timestamp(repo: BYOKRepo, user_id: str):
    repo.store(user_id=user_id, provider=Provider.ANTHROPIC_API, secret="sk-ant-api03-yyy")
    before = repo.metadata(user_id=user_id, provider=Provider.ANTHROPIC_API)
    assert before is not None and before.last_used_at is None
    repo.touch_last_used(user_id=user_id, provider=Provider.ANTHROPIC_API)
    after = repo.metadata(user_id=user_id, provider=Provider.ANTHROPIC_API)
    assert after is not None
    assert after.last_used_at is not None


def test_store_rejects_empty_secret(repo: BYOKRepo, user_id: str):
    with pytest.raises(ValueError):
        repo.store(user_id=user_id, provider=Provider.ANTHROPIC_API, secret="   ")


def test_wrong_master_key_raises_on_read(initialized_db, user_id: str):
    # Store with one key…
    BYOKRepo(master_key=_KEY).store(
        user_id=user_id,
        provider=Provider.ANTHROPIC_API,
        secret="sk-ant-api03-rotated",
    )
    # …then try to read with another. The decrypt MUST raise — we
    # don't want silent "no key" because that masks a misconfig
    # (rotated master, wrong env on a new container, etc.).
    wrong = bytes.fromhex("bb" * 32)
    with pytest.raises(crypto.DecryptionError):
        BYOKRepo(master_key=wrong).get_secret(user_id=user_id, provider=Provider.ANTHROPIC_API)
