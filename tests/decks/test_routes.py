"""HTTP route tests for the decks bounded context.

Drive the router through FastAPI's TestClient — exercises the full
stack from HTTP request → router → service → repo → sqlite. Tests
share the per-test temp-path sqlite + initialized_db fixture.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from prep.decks.entities import NewQuestion, QuestionType
from prep.decks.repo import DeckRepo, QuestionRepo


def _seed_deck(initialized_db: str, name: str = "go-systems", with_questions: int = 0) -> int:
    user = initialized_db
    deck_id = DeckRepo().create(user, name)
    q = QuestionRepo()
    for i in range(with_questions):
        q.add(user, deck_id, NewQuestion(type=QuestionType.MCQ, prompt=f"q{i}", answer="A"))
    return deck_id


def test_delete_deck_happy_path(client: TestClient, initialized_db: str):
    """Form-encoded delete with matching confirm name → deck gone."""
    _seed_deck(initialized_db, name="doomed", with_questions=2)
    r = client.post("/deck/doomed/delete", data={"confirm": "doomed"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].endswith("/")
    # Deck is gone.
    assert DeckRepo().find_id(initialized_db, "doomed") is None


def test_delete_deck_wrong_confirm_400(client: TestClient, initialized_db: str):
    _seed_deck(initialized_db, name="precious")
    r = client.post(
        "/deck/precious/delete",
        data={"confirm": "wrong-name"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    # Deck still exists.
    assert DeckRepo().find_id(initialized_db, "precious") is not None


def test_delete_nonexistent_deck_404(client: TestClient, initialized_db: str):
    r = client.post(
        "/deck/ghost/delete",
        data={"confirm": "ghost"},
        follow_redirects=False,
    )
    assert r.status_code == 404


def test_delete_other_users_deck_404(client: TestClient, initialized_db: str):
    """User isolation: alice can't delete bob's deck even with the
    correct confirm name."""
    from prep import db as _db

    _db.upsert_user("bob@example.com", display_name="Bob")
    DeckRepo().create("bob@example.com", "bobs-deck")
    # The TestClient is authenticated as the env-default user (alice).
    r = client.post(
        "/deck/bobs-deck/delete",
        data={"confirm": "bobs-deck"},
        follow_redirects=False,
    )
    assert r.status_code == 404
    # bob's deck still exists.
    assert DeckRepo().find_id("bob@example.com", "bobs-deck") is not None
