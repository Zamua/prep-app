"""The merge trigger inside `optional_current_user`: when it fires,
what it does to the cookie, and the fact that it can never fail a
request (docs/ANONYMOUS-ACCOUNTS.md section 5)."""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from prep.auth import anon_cookie as ac
from prep.auth import identity as identity_mod
from prep.auth.merge import LeftoverAnonRows, MergeResult
from prep.auth.providers import set_provider
from prep.infrastructure.db import cursor
from tests.anon_support import ANON_ID, MASTER_KEY, seed_anon_user

NEW_LOGIN = "newbie@example.com"
HEADERS = {"Tailscale-User-Login": NEW_LOGIN, "Tailscale-User-Name": "Newbie"}


@pytest.fixture
def anon_client(env: None, monkeypatch: pytest.MonkeyPatch):
    """An app with anonymous accounts enabled and no default-user
    bypass, so identity comes from the Tailscale headers a test sends
    and the anonymous cookie is a real credential."""
    import importlib

    monkeypatch.delenv("PREP_DEFAULT_USER", raising=False)
    monkeypatch.delenv(ac.SECRET_ENV, raising=False)
    monkeypatch.setenv(ac.MASTER_ENV, MASTER_KEY)
    set_provider(None)

    from prep.infrastructure import db as db_mod

    importlib.reload(db_mod)
    from prep import app as app_mod

    importlib.reload(app_mod)

    with TestClient(app_mod.app) as c:
        yield c
    set_provider(None)


def seed_anon_deck(user_id: str = ANON_ID, name: str = "instant-deck") -> None:
    with cursor() as c:
        c.execute(
            "INSERT INTO decks (user_id, name, display_name, created_at)"
            " VALUES (?, ?, 'Instant deck', '2026-01-01T00:00:00+00:00')",
            (user_id, name),
        )


def deck_owner(name: str = "instant-deck") -> str | None:
    with cursor() as c:
        row = c.execute("SELECT user_id FROM decks WHERE name = ?", (name,)).fetchone()
    return row["user_id"] if row else None


def user_rows() -> set[str]:
    with cursor() as c:
        return {r["tailscale_login"] for r in c.execute("SELECT tailscale_login FROM users")}


def merge_count() -> int:
    with cursor() as c:
        return c.execute("SELECT COUNT(*) AS n FROM account_merges").fetchone()["n"]


def cookie_cleared(response) -> bool:
    return any(
        h.startswith(f"{ac.COOKIE_NAME}=") and "max-age=0" in h.lower()
        for h in response.headers.get_list("set-cookie")
    )


def cookie_touched(response) -> bool:
    return any(h.startswith(f"{ac.COOKIE_NAME}=") for h in response.headers.get_list("set-cookie"))


def sign_in(client: TestClient, *, path: str = "/") -> object:
    return client.get(path, headers=HEADERS)


# ---- the ordering regression ------------------------------------------


def test_fresh_signup_with_no_pre_existing_row_merges(anon_client: TestClient):
    """No `users` row for the provider id and no webhook has fired:
    the merge takes the id the upsert just wrote. Running before the
    upsert makes this sign-up a silent total loss."""
    seed_anon_user(ANON_ID)
    seed_anon_deck()
    assert NEW_LOGIN not in user_rows()
    anon_client.cookies.set(ac.COOKIE_NAME, ac.mint_cookie(ANON_ID))

    response = sign_in(anon_client)

    assert response.status_code == 200
    assert deck_owner() == NEW_LOGIN
    assert ANON_ID not in user_rows()
    assert cookie_cleared(response)


def test_the_target_row_exists_when_the_merge_runs(
    anon_client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    seen: list[bool] = []
    real = identity_mod.merge_anonymous_into

    def recording(anon_user_id: str, target_user_id: str) -> MergeResult:
        seen.append(target_user_id in user_rows())
        return real(anon_user_id, target_user_id)

    monkeypatch.setattr(identity_mod, "merge_anonymous_into", recording)
    seed_anon_user(ANON_ID)
    seed_anon_deck()
    anon_client.cookies.set(ac.COOKIE_NAME, ac.mint_cookie(ANON_ID))

    sign_in(anon_client)

    assert seen == [True]


def test_sign_in_to_an_existing_account_merges_too(anon_client: TestClient):
    """The trigger does not distinguish sign-up from sign-in."""
    sign_in(anon_client)  # the account already exists
    seed_anon_user(ANON_ID)
    seed_anon_deck()
    anon_client.cookies.set(ac.COOKIE_NAME, ac.mint_cookie(ANON_ID))

    response = sign_in(anon_client)

    assert deck_owner() == NEW_LOGIN
    assert cookie_cleared(response)


# ---- the cookie policy ------------------------------------------------


def test_a_cookie_naming_nothing_is_cleared(anon_client: TestClient):
    """`anon_missing`: the pointer names nothing, so keeping it buys
    nothing."""
    anon_client.cookies.set(ac.COOKIE_NAME, ac.mint_cookie(ANON_ID))

    response = sign_in(anon_client)

    assert cookie_cleared(response)
    assert merge_count() == 1


def test_a_cookie_naming_the_signed_in_account_is_cleared(anon_client: TestClient):
    """`same_user`: the cookie names the row it is presented on, so
    there is nothing to move and nothing left for it to point at.
    Reachable only if a provider issues an `anon:`-shaped id, which
    `mint_cookie` is the reason none can be forged into."""
    headers = {"Tailscale-User-Login": ANON_ID, "Tailscale-User-Name": "Same"}
    anon_client.cookies.set(ac.COOKIE_NAME, ac.mint_cookie(ANON_ID))

    response = anon_client.get("/", headers=headers)

    assert response.status_code == 200
    assert cookie_cleared(response)
    assert merge_count() == 0


def test_the_cookie_survives_a_non_anonymous_row(anon_client: TestClient):
    """`not_anonymous`: the row is still there holding real decks."""
    with cursor() as c:
        c.execute(
            "INSERT INTO users (tailscale_login, display_name, created_at, last_seen_at,"
            " is_anonymous) VALUES (?, 'Someone', '2026-01-01T00:00:00+00:00',"
            " '2026-01-01T00:00:00+00:00', 0)",
            (ANON_ID,),
        )
    seed_anon_deck(ANON_ID)
    anon_client.cookies.set(ac.COOKIE_NAME, ac.mint_cookie(ANON_ID))

    response = sign_in(anon_client)

    assert response.status_code == 200
    assert not cookie_touched(response)
    assert deck_owner() == ANON_ID


def test_the_cookie_survives_a_target_missing_result(
    anon_client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        identity_mod,
        "merge_anonymous_into",
        lambda anon_id, target_id: MergeResult(
            resolved=False, merged=False, counts={}, reason="target_missing"
        ),
    )
    seed_anon_user(ANON_ID)
    seed_anon_deck()
    anon_client.cookies.set(ac.COOKIE_NAME, ac.mint_cookie(ANON_ID))

    response = sign_in(anon_client)

    assert response.status_code == 200
    assert not cookie_touched(response)
    assert deck_owner() == ANON_ID


@pytest.mark.parametrize(
    "boom",
    [
        sqlite3.OperationalError("database is locked"),
        LeftoverAnonRows("decks still holds 1 row(s)"),
    ],
    ids=["lock-timeout", "internal-guard"],
)
def test_the_cookie_survives_an_exception_and_the_next_request_completes(
    anon_client: TestClient, monkeypatch: pytest.MonkeyPatch, boom: Exception
):
    def raising(anon_user_id: str, target_user_id: str) -> MergeResult:
        raise boom

    real = identity_mod.merge_anonymous_into
    monkeypatch.setattr(identity_mod, "merge_anonymous_into", raising)
    seed_anon_user(ANON_ID)
    seed_anon_deck()
    anon_client.cookies.set(ac.COOKIE_NAME, ac.mint_cookie(ANON_ID))

    failed = sign_in(anon_client)

    assert failed.status_code == 200
    assert not cookie_touched(failed)
    assert deck_owner() == ANON_ID
    assert anon_client.cookies.get(ac.COOKIE_NAME)

    monkeypatch.setattr(identity_mod, "merge_anonymous_into", real)
    retried = sign_in(anon_client)

    assert deck_owner() == NEW_LOGIN
    assert cookie_cleared(retried)


def test_the_merge_never_500s(anon_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    def raising(anon_user_id: str, target_user_id: str) -> MergeResult:
        raise RuntimeError("anything at all")

    monkeypatch.setattr(identity_mod, "merge_anonymous_into", raising)
    seed_anon_user(ANON_ID)
    seed_anon_deck()
    anon_client.cookies.set(ac.COOKIE_NAME, ac.mint_cookie(ANON_ID))

    response = sign_in(anon_client)

    assert response.status_code == 200
    assert "<html" in response.text.lower()


# ---- what must not merge ----------------------------------------------


def test_a_second_request_does_nothing(anon_client: TestClient):
    seed_anon_user(ANON_ID)
    seed_anon_deck()
    anon_client.cookies.set(ac.COOKIE_NAME, ac.mint_cookie(ANON_ID))

    first = sign_in(anon_client)
    assert cookie_cleared(first)
    anon_client.cookies.delete(ac.COOKIE_NAME)  # what the browser does with it

    second = sign_in(anon_client)

    assert not cookie_touched(second)
    assert merge_count() == 1
    assert deck_owner() == NEW_LOGIN


def test_replaying_the_same_cookie_moves_nothing_twice(anon_client: TestClient):
    """A racing tab still holding the cookie lands on the same
    resolved no-op a reap would give."""
    seed_anon_user(ANON_ID)
    seed_anon_deck()
    anon_client.cookies.set(ac.COOKIE_NAME, ac.mint_cookie(ANON_ID))

    sign_in(anon_client)
    replayed = sign_in(anon_client)

    assert cookie_cleared(replayed)
    assert deck_owner() == NEW_LOGIN
    with cursor() as c:
        decks = c.execute("SELECT COUNT(*) AS n FROM decks").fetchone()["n"]
    assert decks == 1


def test_a_cookie_only_request_does_not_merge(anon_client: TestClient):
    """No provider identity means no target: the visitor is still the
    anonymous account, browsing its own decks."""
    seed_anon_user(ANON_ID)
    seed_anon_deck()
    anon_client.cookies.set(ac.COOKIE_NAME, ac.mint_cookie(ANON_ID))

    response = anon_client.get("/")

    assert response.status_code == 200
    assert merge_count() == 0
    assert deck_owner() == ANON_ID


def test_the_clerk_webhook_does_not_merge(env: None, monkeypatch: pytest.MonkeyPatch):
    """It arrives server to server and cannot know which browser
    signed up, so it keeps its own job and gains nothing."""
    import importlib

    monkeypatch.setenv("CLERK_WEBHOOK_SECRET", "whsec_testfake")
    monkeypatch.delenv(ac.SECRET_ENV, raising=False)
    monkeypatch.setenv(ac.MASTER_ENV, MASTER_KEY)
    set_provider(None)

    from prep.infrastructure import db as db_mod

    importlib.reload(db_mod)
    from prep import app as app_mod

    importlib.reload(app_mod)

    payload = {
        "type": "user.created",
        "data": {
            "id": "user_2abc",
            "first_name": "Alice",
            "last_name": "Anderson",
            "image_url": None,
            "email_addresses": [{"id": "idn_1", "email_address": "alice@example.com"}],
            "primary_email_address_id": "idn_1",
        },
    }
    with TestClient(app_mod.app) as client:
        seed_anon_user(ANON_ID)
        seed_anon_deck()
        client.cookies.set(ac.COOKIE_NAME, ac.mint_cookie(ANON_ID))
        with patch("svix.webhooks.Webhook.verify", return_value=payload):
            response = client.post(
                "/webhooks/clerk",
                content=json.dumps(payload),
                headers={"svix-id": "msg", "svix-timestamp": "1", "svix-signature": "v1,fake"},
            )

    assert response.status_code == 200
    assert merge_count() == 0
    assert deck_owner() == ANON_ID
    set_provider(None)
