"""Route tests for GET /api/offline/snapshot (docs/OFFLINE.md
sections 4 and 7, M1 scope).

The snapshot is the authenticated data feed sync.js writes into
IndexedDB: the server-resolved identity, the user's SRS decks, and
every non-suspended card with its SRS view. Auth, scope (SRS-only,
non-suspended-only), and per-user isolation are the contract pinned
here.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from prep.decks.entities import NewQuestion, QuestionType
from prep.decks.repo import DeckRepo, QuestionRepo


def test_snapshot_requires_auth(unauthed_client: TestClient):
    """No identity, no snapshot: the endpoint uses the standard
    current_user dependency, unlike the deliberately public /offline
    shell."""
    r = unauthed_client.get("/api/offline/snapshot")
    assert r.status_code == 401


@pytest.fixture
def snapshot_seed(initialized_db: str) -> dict:
    """One SRS deck with an mcq and a short card for the default test
    user. Returns the ids the assertions need."""
    user = initialized_db
    deck_repo = DeckRepo()
    q_repo = QuestionRepo()
    deck_id = deck_repo.create(user, "capitals", display_name="Capitals")
    mcq_id = q_repo.add(
        user,
        deck_id,
        NewQuestion(
            type=QuestionType.MCQ,
            prompt="Capital of France?",
            answer="Paris",
            choices=["Paris", "Lyon"],
        ),
    )
    short_id = q_repo.add(
        user,
        deck_id,
        NewQuestion(type=QuestionType.SHORT, prompt="Capital of Peru?", answer="Lima"),
    )
    return {"user": user, "deck_id": deck_id, "mcq_id": mcq_id, "short_id": short_id}


def test_snapshot_shape(client: TestClient, snapshot_seed: dict):
    """The implemented payload shape: identity + decks + cards, cards
    carrying the question fields the offline grader needs plus the
    coarse SRS view (step bucket + next_due)."""
    r = client.get("/api/offline/snapshot")
    assert r.status_code == 200
    payload = r.json()

    assert set(payload) == {"user", "generated_at", "decks", "cards"}
    assert payload["user"]["id"] == snapshot_seed["user"]
    assert payload["user"]["display_name"]
    assert isinstance(payload["generated_at"], str)

    decks = payload["decks"]
    assert [d["id"] for d in decks] == [snapshot_seed["deck_id"]]
    assert decks[0]["name"] == "capitals"
    assert decks[0]["display_name"] == "Capitals"

    cards = {c["question_id"]: c for c in payload["cards"]}
    assert set(cards) == {snapshot_seed["mcq_id"], snapshot_seed["short_id"]}

    mcq = cards[snapshot_seed["mcq_id"]]
    assert mcq["deck_id"] == snapshot_seed["deck_id"]
    assert mcq["type"] == "mcq"
    assert mcq["prompt"] == "Capital of France?"
    # choices decode to a real list at the repo boundary.
    assert mcq["choices"] == ["Paris", "Lyon"]
    assert mcq["answer"] == "Paris"
    # A fresh card sits at the bottom maturity bucket and is due.
    assert mcq["step"] == 0
    assert isinstance(mcq["next_due"], str) and mcq["next_due"]

    short = cards[snapshot_seed["short_id"]]
    assert short["type"] == "short"
    assert short["choices"] is None
    # Optional grading fields are present (null when unset), so the
    # client store's schema never sees missing keys.
    for key in ("answer_regex", "rubric", "skeleton", "explanation"):
        assert key in short


def test_snapshot_excludes_suspended_cards(client: TestClient, snapshot_seed: dict):
    """Suspended questions are out of the study rotation online; the
    offline queue must match."""
    QuestionRepo().set_suspended(snapshot_seed["user"], snapshot_seed["mcq_id"], True)
    r = client.get("/api/offline/snapshot")
    assert r.status_code == 200
    ids = [c["question_id"] for c in r.json()["cards"]]
    assert snapshot_seed["mcq_id"] not in ids
    assert snapshot_seed["short_id"] in ids


def test_snapshot_excludes_trivia_decks(client: TestClient, snapshot_seed: dict):
    """Offline covers SRS decks only (docs/OFFLINE.md non-goals):
    trivia decks and their questions never enter the snapshot."""
    user = snapshot_seed["user"]
    trivia_id = DeckRepo().create_trivia(
        user, "daily-trivia", topic="anything", interval_minutes=60
    )
    trivia_q = QuestionRepo().add(
        user,
        trivia_id,
        NewQuestion(type=QuestionType.MCQ, prompt="trivia?", answer="yes", choices=["yes", "no"]),
    )

    r = client.get("/api/offline/snapshot")
    assert r.status_code == 200
    payload = r.json()
    assert trivia_id not in [d["id"] for d in payload["decks"]]
    assert trivia_q not in [c["question_id"] for c in payload["cards"]]


def test_snapshot_is_user_scoped(client: TestClient, snapshot_seed: dict):
    """IDOR discipline, both directions: another user's decks and
    cards never appear in the default user's snapshot, and a request
    authenticated as that other user sees only their own."""
    from prep.auth.repo import UserRepo

    other = "bob@example.com"
    UserRepo().upsert(other, display_name="Bob")
    other_deck = DeckRepo().create(other, "bobs-deck")
    other_q = QuestionRepo().add(
        other,
        other_deck,
        NewQuestion(type=QuestionType.SHORT, prompt="bob's card", answer="secret"),
    )

    # Default user's snapshot: bob's rows are invisible.
    payload = client.get("/api/offline/snapshot").json()
    assert other_deck not in [d["id"] for d in payload["decks"]]
    assert other_q not in [c["question_id"] for c in payload["cards"]]

    # Bob's snapshot (Tailscale headers win over PREP_DEFAULT_USER):
    # only bob's rows, none of the default user's.
    r = client.get(
        "/api/offline/snapshot",
        headers={"Tailscale-User-Login": other, "Tailscale-User-Name": "Bob"},
    )
    assert r.status_code == 200
    bob_payload = r.json()
    assert bob_payload["user"]["id"] == other
    assert [d["id"] for d in bob_payload["decks"]] == [other_deck]
    assert [c["question_id"] for c in bob_payload["cards"]] == [other_q]
