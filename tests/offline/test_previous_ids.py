"""`user.previous_ids` on GET /api/offline/snapshot: what tells a
merged device it is still looking at its own data
(docs/ANONYMOUS-ACCOUNTS.md section 5)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from prep.auth.merge import merge_anonymous_into
from prep.auth.repo import UserRepo
from tests.anon_support import seed_anon_user

ANON = "anon:" + "ab" * 16
OTHER_ANON = "anon:" + "cd" * 16


def test_an_unmerged_account_carries_an_empty_list(client: TestClient, initialized_db: str):
    payload = client.get("/api/offline/snapshot").json()
    assert payload["user"]["id"] == initialized_db
    assert payload["user"]["previous_ids"] == []


def test_a_merged_anon_id_reaches_the_client(client: TestClient, initialized_db: str):
    seed_anon_user(ANON)
    seed_anon_user(OTHER_ANON)
    assert merge_anonymous_into(ANON, initialized_db).resolved
    assert merge_anonymous_into(OTHER_ANON, initialized_db).resolved

    payload = client.get("/api/offline/snapshot").json()

    assert payload["user"]["previous_ids"] == [ANON, OTHER_ANON]


def test_a_client_supplied_previous_ids_is_ignored(client: TestClient, initialized_db: str):
    """The list is derived from the merge audit and from nothing else.
    A client naming an id it would like to own gets the server's
    answer on the read, and the write has no field to carry one."""
    seed_anon_user(ANON)

    forged = client.get("/api/offline/snapshot", params={"previous_ids": ANON})
    assert forged.json()["user"]["previous_ids"] == []

    posted = client.post(
        "/api/offline/sync",
        json={"device_id": "d1", "new_cards": [], "reviews": [], "previous_ids": [ANON]},
    )
    assert posted.status_code == 200

    assert client.get("/api/offline/snapshot").json()["user"]["previous_ids"] == []


def test_a_third_users_merge_never_appears_in_someone_elses_snapshot(
    client: TestClient, initialized_db: str
):
    other = UserRepo().upsert("bob@example.com", display_name="Bob")["tailscale_login"]
    seed_anon_user(ANON)
    assert merge_anonymous_into(ANON, other).merged

    payload = client.get("/api/offline/snapshot").json()

    assert payload["user"]["id"] == initialized_db
    assert payload["user"]["previous_ids"] == []
