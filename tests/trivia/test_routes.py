"""HTTP route tests for the trivia bounded context.

Exercise the card view, answer submission, and (mocked) generate
endpoint via TestClient. Generation tests stub `run_prompt` so we
don't shell out to claude.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from prep.decks.entities import NewQuestion, QuestionType
from prep.decks.repo import DeckRepo, QuestionRepo
from prep.trivia import service as svc
from prep.trivia.repo import TriviaQueueRepo


def _seed_trivia_question(initialized_db: str, prompt="Capital of France?", answer="Paris"):
    user = initialized_db
    deck_id = DeckRepo().create(user, "capitals")
    qid = QuestionRepo().add(
        user,
        deck_id,
        NewQuestion(type=QuestionType.SHORT, topic="capitals", prompt=prompt, answer=answer),
    )
    TriviaQueueRepo().append_card(qid, deck_id)
    return deck_id, qid


# ---- GET /trivia/<id> --------------------------------------------------


def test_card_view_renders_prompt(client: TestClient, initialized_db: str):
    _, qid = _seed_trivia_question(initialized_db)
    r = client.get(f"/trivia/{qid}")
    assert r.status_code == 200
    assert "Capital of France?" in r.text
    # No result block yet — only the form.
    assert "trivia-answer-form" in r.text
    assert "trivia-result" not in r.text


def test_card_view_404_for_unknown_question(client: TestClient, initialized_db: str):
    r = client.get("/trivia/99999")
    assert r.status_code == 404


# ---- POST /trivia/<id>/answer ------------------------------------------


def test_answer_correct_renders_result_block(client: TestClient, initialized_db: str):
    _, qid = _seed_trivia_question(initialized_db)
    r = client.post(f"/trivia/{qid}/answer", data={"answer": "paris"})
    assert r.status_code == 200
    assert "trivia-result-right" in r.text
    assert "Correct" in r.text
    # Card was rotated — count_unanswered should now be 0.
    assert TriviaQueueRepo().count_unanswered(deck_id=1) == 0


def test_answer_wrong_shows_correct_answer(client: TestClient, initialized_db: str):
    _, qid = _seed_trivia_question(initialized_db)
    r = client.post(f"/trivia/{qid}/answer", data={"answer": "london"})
    assert r.status_code == 200
    assert "trivia-result-wrong" in r.text
    # Correct answer is shown so the user can learn from the miss.
    assert "Paris" in r.text


def test_answer_blank_grades_wrong(client: TestClient, initialized_db: str):
    _, qid = _seed_trivia_question(initialized_db)
    r = client.post(f"/trivia/{qid}/answer", data={"answer": ""})
    assert r.status_code == 200
    assert "trivia-result-wrong" in r.text


# ---- POST /trivia/decks/<id>/generate ----------------------------------


def test_generate_route_inserts_via_mocked_agent(
    monkeypatch, client: TestClient, initialized_db: str
):
    """Generate route calls service.generate_batch, which calls the
    agent. We monkey-patch the agent call to return canned JSON."""
    user = initialized_db
    deck_id = DeckRepo().create(user, "history", context_prompt="World War II turning points")
    monkeypatch.setattr(svc, "run_prompt", lambda _p: '[{"q": "Year of D-Day?", "a": "1944"}]')
    r = client.post(f"/trivia/decks/{deck_id}/generate", follow_redirects=False)
    assert r.status_code == 200
    # The stub redirect page mentions the count.
    assert "Generated 1" in r.text
    # And the question landed in the queue.
    assert TriviaQueueRepo().pick_next_for_deck(deck_id).prompt == "Year of D-Day?"


def test_generate_route_404_for_unknown_deck(client: TestClient, initialized_db: str):
    r = client.post("/trivia/decks/99999/generate", follow_redirects=False)
    assert r.status_code == 404


# ---- /decks/new chooser + /decks/new/trivia ----------------------------


def test_decks_new_chooser_offers_both_paths(client: TestClient, initialized_db: str):
    """The chooser page links to both type-specific forms."""
    r = client.get("/decks/new")
    assert r.status_code == 200
    assert "/decks/new/srs" in r.text
    assert "/decks/new/trivia" in r.text


def test_decks_new_trivia_form_renders(client: TestClient, initialized_db: str):
    r = client.get("/decks/new/trivia")
    assert r.status_code == 200
    assert "Topic" in r.text
    assert 'name="topic"' in r.text
    assert 'name="notification_interval_minutes"' in r.text


def test_decks_new_trivia_creates_deck_and_generates(
    monkeypatch, client: TestClient, initialized_db: str
):
    """POST /decks/new/trivia creates a deck_type='trivia' deck, fires
    the initial batch (claude mocked), redirects to the deck page."""
    import prep.agent

    prep.agent.is_available = True
    monkeypatch.setattr(
        "prep.trivia.service.run_prompt",
        lambda _p: '[{"q": "Capital of Italy?", "a": "Rome"}]',
    )
    r = client.post(
        "/decks/new/trivia",
        data={
            "name": "geo",
            "topic": "world capitals",
            "notification_interval_minutes": "15",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].endswith("/deck/geo")
    # Deck exists with deck_type='trivia' and the right interval.
    rows = DeckRepo().list_trivia_decks()
    geo = next(d for d in rows if d["name"] == "geo")
    assert geo["notification_interval_minutes"] == 15
    # And the initial batch is in the queue.
    nxt = TriviaQueueRepo().pick_next_for_deck(geo["id"])
    assert nxt.prompt == "Capital of Italy?"


def test_decks_new_trivia_rejects_empty_topic(monkeypatch, client: TestClient, initialized_db: str):
    import prep.agent

    prep.agent.is_available = True
    r = client.post(
        "/decks/new/trivia",
        data={
            "name": "geo",
            "topic": "",
            "notification_interval_minutes": "30",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "topic" in r.text.lower()
