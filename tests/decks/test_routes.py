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


# ---- suspend / unsuspend -----------------------------------------------


def test_suspend_then_unsuspend_via_http(client: TestClient, initialized_db: str):
    user = initialized_db
    deck_id = DeckRepo().create(user, "deck-a")
    qid = QuestionRepo().add(
        user, deck_id, NewQuestion(type=QuestionType.MCQ, prompt="?", answer="A")
    )

    # suspend
    r = client.post(f"/question/{qid}/suspend", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].endswith("/deck/deck-a")
    assert QuestionRepo().get(user, qid).suspended is True

    # unsuspend
    r = client.post(f"/question/{qid}/unsuspend", follow_redirects=False)
    assert r.status_code == 303
    assert QuestionRepo().get(user, qid).suspended is False


def test_suspend_404_on_missing_question(client: TestClient, initialized_db: str):
    r = client.post("/question/999999/suspend", follow_redirects=False)
    assert r.status_code == 404


def test_suspend_other_users_question_404(client: TestClient, initialized_db: str):
    """User isolation again — alice can't suspend bob's questions."""
    from prep import db as _db

    _db.upsert_user("bob@example.com")
    deck_id = DeckRepo().create("bob@example.com", "bobs-deck")
    qid = QuestionRepo().add(
        "bob@example.com", deck_id, NewQuestion(type=QuestionType.MCQ, prompt="?", answer="A")
    )
    r = client.post(f"/question/{qid}/suspend", follow_redirects=False)
    assert r.status_code == 404
    # bob's question is still unsuspended.
    assert QuestionRepo().get("bob@example.com", qid).suspended is False


# ---- deck view ---------------------------------------------------------


def test_deck_view_renders_for_existing_deck(client: TestClient, initialized_db: str):
    _seed_deck(initialized_db, name="reading-list", with_questions=2)
    r = client.get("/deck/reading-list")
    assert r.status_code == 200
    # Template fields surface — deck name + at least one of the question
    # prompts should appear in the rendered HTML.
    assert "reading-list" in r.text
    assert "q0" in r.text
    assert "q1" in r.text


def test_deck_view_lazy_materializes_empty_deck(client: TestClient, initialized_db: str):
    """get_or_create_deck behavior: hitting /deck/<new-name> creates
    the deck row on demand, then renders the empty-deck UI."""
    user = initialized_db
    assert DeckRepo().find_id(user, "fresh-deck") is None
    r = client.get("/deck/fresh-deck")
    assert r.status_code == 200
    assert DeckRepo().find_id(user, "fresh-deck") is not None


def test_deck_view_hides_study_button_for_trivia(client: TestClient, initialized_db: str):
    """Trivia decks are notification-driven — the deck page should
    surface that fact instead of the Begin study session button."""
    DeckRepo().create_trivia(initialized_db, "geo", topic="capitals", interval_minutes=30)
    r = client.get("/deck/geo")
    assert r.status_code == 200
    assert "Begin study session" not in r.text
    assert "answered via notifications" in r.text


def test_study_begin_400_for_trivia_deck(client: TestClient, initialized_db: str):
    """A stale bookmark shouldn't be able to start an SRS session
    against a trivia deck."""
    DeckRepo().create_trivia(initialized_db, "geo", topic="capitals", interval_minutes=30)
    r = client.post("/study/geo/begin", follow_redirects=False)
    assert r.status_code == 400
    assert "trivia" in r.text.lower()
