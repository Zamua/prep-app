"""HTTP route tests for the trivia bounded context.

Exercise the card view, answer submission, and (mocked) generate
endpoint via TestClient. Generation tests stub `run_prompt` so we
don't shell out to claude.
"""

from __future__ import annotations

import pytest
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
    # Verdict pill in the nav row carries the verdict label.
    assert "tvp-right" in r.text
    assert "correct" in r.text
    # Card was rotated — count_unanswered should now be 0.
    assert TriviaQueueRepo().count_unanswered(deck_id=1) == 0


def test_answer_renders_explain_disclosure_when_explanation_present(
    client: TestClient, initialized_db: str
):
    """If the question carries an explanation, the disc-row exposes
    the Explain pill with the explanation text behind it. Hidden when
    explanation is null (legacy cards)."""
    user = initialized_db
    deck_id = DeckRepo().create(user, "history")
    qid = QuestionRepo().add(
        user,
        deck_id,
        NewQuestion(
            type=QuestionType.SHORT,
            topic="history",
            prompt="Who painted the Mona Lisa?",
            answer="Leonardo da Vinci",
            explanation="Painted around 1503-1519, the Mona Lisa is a portrait of Lisa Gherardini.",
        ),
    )
    TriviaQueueRepo().append_card(qid, deck_id)
    r = client.post(f"/trivia/{qid}/answer", data={"answer": "leonardo"})
    assert r.status_code == 200
    assert "trivia-disc-row" in r.text
    # New label: "Explain" (renamed from "Deep dive").
    assert ">Explain<" in r.text
    assert "Painted around 1503-1519" in r.text


def test_answer_omits_explain_when_no_explanation_and_no_handoff(
    client: TestClient, initialized_db: str
):
    """When neither explain nor explore content is available the
    whole disc-row is omitted."""
    _, qid = _seed_trivia_question(initialized_db)
    r = client.post(f"/trivia/{qid}/answer", data={"answer": "paris"})
    assert r.status_code == 200
    # The Explain pill should not appear (no explanation).
    assert ">Explain<" not in r.text


def test_answer_renders_explore_further_with_chat_and_google(
    client: TestClient, initialized_db: str
):
    """After submitting, trivia cards should expose 'Explore further':
    Claude/ChatGPT prefilled chat URLs PLUS a Google search link.
    All three open in new tabs (target=_blank → native browser on iOS PWA)."""
    _, qid = _seed_trivia_question(initialized_db)
    r = client.post(f"/trivia/{qid}/answer", data={"answer": "paris"})
    assert r.status_code == 200
    assert "Explore further" in r.text
    assert "trivia-explore-option" in r.text
    assert "Discuss with Claude" in r.text
    assert "Search on Google" in r.text
    assert "https://www.google.com/search?q=" in r.text
    # All three trivia explore links must open in a fresh browser context.
    assert r.text.count('target="_blank"') >= 3


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


def test_signal_routes_call_temporal_helpers(monkeypatch, client: TestClient, initialized_db: str):
    """The plan-review UI hits /trivia/gen/<wid>/feedback, /accept,
    /reject. Each route should validate ownership, then forward to
    the matching temporal_client.signal_trivia_* helper."""
    deck_id = DeckRepo().create_trivia(
        initialized_db, "doom", topic="doom 1993", interval_minutes=30
    )
    assert deck_id  # smoke
    wid = "trivia-doom-deadbeef01"
    calls: list[tuple[str, tuple]] = []

    async def fake_feedback(workflow_id, fb):
        calls.append(("feedback", (workflow_id, fb)))

    async def fake_accept(workflow_id):
        calls.append(("accept", (workflow_id,)))

    async def fake_reject(workflow_id):
        calls.append(("reject", (workflow_id,)))

    from prep import temporal_client as _tc

    monkeypatch.setattr(_tc, "signal_trivia_feedback", fake_feedback)
    monkeypatch.setattr(_tc, "signal_trivia_accept", fake_accept)
    monkeypatch.setattr(_tc, "signal_trivia_reject", fake_reject)

    # Feedback
    r = client.post(f"/trivia/gen/{wid}/feedback", data={"feedback": "go deeper on multiplayer"})
    assert r.status_code == 200
    assert calls[-1] == ("feedback", (wid, "go deeper on multiplayer"))

    # Accept
    r = client.post(f"/trivia/gen/{wid}/accept")
    assert r.status_code == 200
    assert calls[-1] == ("accept", (wid,))

    # Reject
    r = client.post(f"/trivia/gen/{wid}/reject")
    assert r.status_code == 200
    assert calls[-1] == ("reject", (wid,))


def test_signal_routes_404_for_unknown_deck(monkeypatch, client: TestClient, initialized_db: str):
    """Workflow id parses as `trivia-<deck>-<rand>`; if the deck isn't
    owned by the user (or doesn't exist) every signal route 404s
    before touching temporal."""
    from prep import temporal_client as _tc

    monkeypatch.setattr(_tc, "signal_trivia_accept", lambda _: pytest.fail("should not call"))
    r = client.post("/trivia/gen/trivia-nonexistent-deadbeef01/accept")
    assert r.status_code == 404


def test_feedback_route_400_on_empty(client: TestClient, initialized_db: str):
    DeckRepo().create_trivia(initialized_db, "doom", topic="doom", interval_minutes=30)
    r = client.post("/trivia/gen/trivia-doom-deadbeef01/feedback", data={"feedback": ""})
    assert r.status_code == 400


def test_decks_new_trivia_creates_deck_and_starts_workflow(
    monkeypatch, client: TestClient, initialized_db: str
):
    """POST /decks/new/trivia creates the deck (sync) + kicks off the
    TriviaGenerateWorkflow (async) and redirects to the polling page.
    The workflow starter is monkey-patched so we don't need a real
    Temporal server in the test loop."""
    import prep.agent
    from prep import temporal_client as _tc

    prep.agent.is_available = True

    async def fake_start(**kwargs):
        return _tc.StartResult(workflow_id=f"trivia-{kwargs['deck_name']}-deadbeef01", run_id="r")

    monkeypatch.setattr(_tc, "start_trivia_generate", fake_start)
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
    assert r.headers["location"].endswith("/trivia/gen/trivia-geo-deadbeef01")
    # Deck row landed sync with deck_type='trivia' and the right interval.
    rows = DeckRepo().list_trivia_decks()
    geo = next(d for d in rows if d["name"] == "geo")
    assert geo["notification_interval_minutes"] == 15
    # Queue starts empty (workflow does the inserts; we faked it).
    assert TriviaQueueRepo().pick_next_for_deck(geo["id"]) is None


# ---- /trivia/session/<deck_name> --------------------------------------


def _seed_n_trivia_questions(initialized_db: str, deck_name: str, n: int) -> tuple[int, list[int]]:
    user = initialized_db
    deck_id = DeckRepo().create_trivia(user, deck_name, topic=deck_name, interval_minutes=30)
    qids: list[int] = []
    for i in range(n):
        qid = QuestionRepo().add(
            user,
            deck_id,
            NewQuestion(type=QuestionType.SHORT, topic=deck_name, prompt=f"Q{i}?", answer=f"A{i}"),
        )
        TriviaQueueRepo().append_card(qid, deck_id)
        qids.append(qid)
    return deck_id, qids


def test_session_no_cards_param_picks_and_redirects(client: TestClient, initialized_db: str):
    """First hit on /trivia/session/<deck>: server picks 3, encodes
    the queue into the URL, returns a 303 to the new URL."""
    _, qids = _seed_n_trivia_questions(initialized_db, "geo", 5)
    r = client.get("/trivia/session/geo", follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "/trivia/session/geo?cards=" in loc
    for qid in qids[:3]:
        assert str(qid) in loc


def test_session_renders_head_card_with_progress(client: TestClient, initialized_db: str):
    """With ?cards=A,B,C the route renders A as the first of 3."""
    _, qids = _seed_n_trivia_questions(initialized_db, "geo", 3)
    csv = ",".join(str(q) for q in qids)
    r = client.get(f"/trivia/session/geo?cards={csv}")
    assert r.status_code == 200
    assert "Q0?" in r.text
    assert "1 of 3" in r.text
    # Hidden cards field carries the FULL queue forward to the answer endpoint.
    assert f'value="{csv}"' in r.text


def test_session_answer_pops_head_and_continues(client: TestClient, initialized_db: str):
    """POST /trivia/session/<deck>/answer with the queue:
    grades + marks_answered + the redirect/render carries the popped queue."""
    _, qids = _seed_n_trivia_questions(initialized_db, "geo", 3)
    csv = ",".join(str(q) for q in qids)
    r = client.post(
        "/trivia/session/geo/answer",
        data={"cards": csv, "answer": "A0"},
    )
    assert r.status_code == 200
    # Verdict block rendered with correct=true.
    assert "trivia-result-right" in r.text
    # Next link carries the popped queue.
    expected_remaining = ",".join(str(q) for q in qids[1:])
    assert f"?cards={expected_remaining}" in r.text


def test_session_empty_cards_renders_done(client: TestClient, initialized_db: str):
    _seed_n_trivia_questions(initialized_db, "geo", 1)
    r = client.get("/trivia/session/geo?cards=")
    assert r.status_code == 200
    assert "Session complete" in r.text


def test_session_skips_foreign_card_id(client: TestClient, initialized_db: str):
    """A bogus question_id in the URL (someone else's, or stale)
    pops without rendering — no IDOR leak, session continues."""
    _, qids = _seed_n_trivia_questions(initialized_db, "geo", 2)
    # 99999 isn't a real question.
    csv = f"99999,{qids[0]}"
    r = client.get(f"/trivia/session/geo?cards={csv}", follow_redirects=False)
    assert r.status_code == 303
    # 303 redirect drops the bogus head and continues with qids[0].
    assert f"cards={qids[0]}" in r.headers["location"]


def test_session_404_for_unknown_deck(client: TestClient, initialized_db: str):
    r = client.get("/trivia/session/nonexistent")
    assert r.status_code == 404


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
