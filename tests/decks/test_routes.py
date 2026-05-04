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
    from prep.auth.repo import UserRepo

    UserRepo().upsert("bob@example.com", display_name="Bob")
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
    from prep.auth.repo import UserRepo

    UserRepo().upsert("bob@example.com")
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
    omit the Begin button entirely. The deck-type tag in the header
    communicates the type."""
    DeckRepo().create_trivia(initialized_db, "geo", topic="capitals", interval_minutes=30)
    r = client.get("/deck/geo")
    assert r.status_code == 200
    assert "Begin study session" not in r.text
    assert "tag-decktype-trivia" in r.text


def test_deck_view_shows_decktype_tag_for_srs(client: TestClient, initialized_db: str):
    DeckRepo().create(initialized_db, "regular-srs")
    r = client.get("/deck/regular-srs")
    assert r.status_code == 200
    assert "tag-decktype-srs" in r.text


def test_trivia_deck_renders_mastery_bar_with_breakdown(client: TestClient, initialized_db: str):
    """Trivia decks swap the 'n due now' meta line for the mastery bar
    + breakdown chips. Seed 4 cards in mixed states to verify the
    aggregates."""
    from prep.decks.entities import NewQuestion, QuestionType
    from prep.trivia.repo import TriviaQueueRepo

    user = initialized_db
    deck_id = DeckRepo().create_trivia(user, "geo", topic="capitals", interval_minutes=30)
    qrepo = QuestionRepo()
    trivia = TriviaQueueRepo()
    qids = []
    for i in range(4):
        qid = qrepo.add(
            user, deck_id, NewQuestion(type=QuestionType.SHORT, prompt=f"Q{i}?", answer=f"A{i}")
        )
        trivia.append_card(qid, deck_id)
        qids.append(qid)
    # 1 mastered, 1 wrong, 2 unanswered.
    trivia.mark_answered(qids[0], correct=True)
    trivia.mark_answered(qids[1], correct=False)

    r = client.get("/deck/geo")
    assert r.status_code == 200
    # SRS-style "due now" line is omitted for trivia decks.
    assert "due now" not in r.text
    # Mastery bar markup present, with computed counts in the labels.
    assert "deck-mastery-bar" in r.text
    assert "1 of 4 mastered" in r.text
    assert "25%" in r.text  # 1/4 → 25%
    assert "2 unanswered" in r.text
    assert "1 wrong" in r.text


def test_deck_view_renders_edit_pill_and_hidden_panel(client: TestClient, initialized_db: str):
    """The pencil-pill in the header controls a hidden #deck-edit-panel
    via aria-controls. The add/transform forms live inside that panel,
    not laid out on the page."""
    DeckRepo().create(initialized_db, "go-systems")
    r = client.get("/deck/go-systems")
    assert r.status_code == 200
    # Pencil pill is in the header pill row.
    assert "edit-pill" in r.text
    assert "data-edit-toggle" in r.text
    assert 'aria-controls="deck-edit-panel"' in r.text
    # Panel exists, holds the Add-by-hand button, ships hidden.
    assert 'id="deck-edit-panel"' in r.text
    assert "Add a card by hand" in r.text  # still in DOM, just hidden
    # `hidden` attribute appears on the panel section.
    panel_idx = r.text.find('id="deck-edit-panel"')
    assert "hidden" in r.text[panel_idx : panel_idx + 200]


def test_trivia_deck_edit_panel_shows_topic_editor_not_add_cards(
    client: TestClient, initialized_db: str
):
    """Trivia decks swap the SRS edit panel (add card / transform) for
    a topic-prompt editor — that's the only knob that affects future
    batches. Manual card add doesn't fit the trivia model."""
    DeckRepo().create_trivia(
        initialized_db, "geo-trivia", topic="capital cities of europe", interval_minutes=30
    )
    r = client.get("/deck/geo-trivia")
    assert r.status_code == 200
    # SRS-only UI is gone for trivia decks.
    assert "Add a card by hand" not in r.text
    # Topic editor is in the panel with the current context_prompt
    # populated as the textarea value.
    assert "Edit topic" in r.text
    assert 'name="context_prompt"' in r.text
    assert "capital cities of europe" in r.text
    # Form posts to the new /topic endpoint.
    assert "/deck/geo-trivia/topic" in r.text


def test_topic_update_persists_for_trivia_deck(client: TestClient, initialized_db: str):
    """POSTing a fresh context_prompt updates the deck row; subsequent
    batch generations read the new value via DeckRepo.get_context_prompt."""
    DeckRepo().create_trivia(
        initialized_db, "geo-trivia", topic="europe capitals", interval_minutes=30
    )
    r = client.post(
        "/deck/geo-trivia/topic",
        data={"context_prompt": "south american capitals + their founding dates"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/deck/geo-trivia" in r.headers["location"]
    stored = DeckRepo().get_context_prompt(initialized_db, "geo-trivia")
    assert stored == "south american capitals + their founding dates"


def test_topic_update_400s_on_srs_deck(client: TestClient, initialized_db: str):
    """SRS decks don't have a topic prompt in this sense — reject."""
    DeckRepo().create(initialized_db, "go-systems")
    r = client.post(
        "/deck/go-systems/topic",
        data={"context_prompt": "irrelevant"},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_topic_update_400s_on_empty_prompt(client: TestClient, initialized_db: str):
    """Empty topic for trivia is rejected — generation needs SOMETHING
    to work with, and silently leaving the prior topic would be
    surprising given the form submitted blank."""
    DeckRepo().create_trivia(initialized_db, "geo-trivia", topic="x", interval_minutes=30)
    r = client.post(
        "/deck/geo-trivia/topic",
        data={"context_prompt": "   "},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_topic_update_404s_for_unknown_deck(client: TestClient, initialized_db: str):
    r = client.post(
        "/deck/no-such-deck/topic",
        data={"context_prompt": "anything"},
        follow_redirects=False,
    )
    assert r.status_code == 404


def test_index_decks_carry_decktype(client: TestClient, initialized_db: str):
    """The index list also shows the type tag — same slot, same chrome,
    consistent across views."""
    DeckRepo().create(initialized_db, "an-srs-deck")
    DeckRepo().create_trivia(initialized_db, "a-trivia-deck", topic="x", interval_minutes=30)
    r = client.get("/")
    assert r.status_code == 200
    assert "tag-decktype-srs" in r.text
    assert "tag-decktype-trivia" in r.text


def test_index_trivia_deck_shows_mastery_mini_bar(client: TestClient, initialized_db: str):
    """Trivia decks render the mini mastery bar instead of the SRS
    "n due now · m total" line, since trivia has no SRS schedule.
    Bar reflects mastered + wrong + unanswered counts."""
    from prep.decks.entities import NewQuestion, QuestionType
    from prep.decks.repo import QuestionRepo
    from prep.trivia.repo import TriviaQueueRepo

    deck_id = DeckRepo().create_trivia(
        initialized_db, "design-interview", topic="x", interval_minutes=30
    )
    qrepo = QuestionRepo()
    trepo = TriviaQueueRepo()
    qids = []
    for i in range(4):
        qid = qrepo.add(
            initialized_db,
            deck_id,
            NewQuestion(type=QuestionType.SHORT, prompt=f"Q{i}?", answer=f"A{i}", topic="x"),
        )
        trepo.append_card(qid, deck_id)
        qids.append(qid)
    trepo.mark_answered(qids[0], correct=True)
    trepo.mark_answered(qids[1], correct=False)
    # qids[2], qids[3] stay unanswered.

    r = client.get("/")
    assert r.status_code == 200
    # Mini bar renders for the trivia deck.
    assert "deck-mastery-mini" in r.text
    # Caption shows mastered / total — 1 right, 4 total.
    assert ">1<" in r.text and "/4 mastered" in r.text


def test_index_srs_deck_keeps_due_total_line(client: TestClient, initialized_db: str):
    """SRS decks keep the original 'n due now · m total' line — no
    mastery bar, since SRS has no equivalent grouping."""
    DeckRepo().create(initialized_db, "concurrency")
    r = client.get("/")
    assert r.status_code == 200
    assert "due now" in r.text
    assert "in deck" in r.text


def test_study_begin_400_for_trivia_deck(client: TestClient, initialized_db: str):
    """A stale bookmark shouldn't be able to start an SRS session
    against a trivia deck."""
    DeckRepo().create_trivia(initialized_db, "geo", topic="capitals", interval_minutes=30)
    r = client.post("/study/geo/begin", follow_redirects=False)
    assert r.status_code == 400
    assert "trivia" in r.text.lower()


def test_deck_view_shows_notifications_toggle_for_trivia(client: TestClient, initialized_db: str):
    """Trivia deck page should expose the pause/resume pill with
    interval info when active."""
    DeckRepo().create_trivia(initialized_db, "geo", topic="capitals", interval_minutes=15)
    r = client.get("/deck/geo")
    assert r.status_code == 200
    assert "notif-pill" in r.text
    assert "every 15m" in r.text
    assert "notif-pill-paused" not in r.text


def test_trivia_notifications_toggle_off_then_on(client: TestClient, initialized_db: str):
    """Posting enabled=off persists to the column; posting on flips
    back. The pill state on the deck page tracks it."""
    deck_id = DeckRepo().create_trivia(initialized_db, "geo", topic="capitals", interval_minutes=30)
    # Off
    r = client.post(
        f"/trivia/decks/{deck_id}/notifications",
        data={"enabled": "off"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    page = client.get("/deck/geo").text
    assert "notif-pill-paused" in page
    assert ">paused<" in page
    # On
    r = client.post(
        f"/trivia/decks/{deck_id}/notifications",
        data={"enabled": "on"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    page = client.get("/deck/geo").text
    assert "notif-pill-paused" not in page
    assert "every 30m" in page


def test_trivia_notifications_toggle_404_for_unknown_deck(client: TestClient, initialized_db: str):
    r = client.post(
        "/trivia/decks/99999/notifications",
        data={"enabled": "off"},
        follow_redirects=False,
    )
    assert r.status_code == 404


def test_deck_notifications_toggle_works_for_srs_deck(client: TestClient, initialized_db: str):
    """SRS decks now also expose the per-deck notification toggle.
    Pausing redirects back to the deck page, pill flips to paused state."""
    DeckRepo().create(initialized_db, "regular-srs")
    r = client.post(
        "/deck/regular-srs/notifications",
        data={"enabled": "off"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    page = client.get("/deck/regular-srs").text
    assert "notif-pill-paused" in page
    assert ">paused<" in page


def test_deck_notifications_toggle_404_for_unknown(client: TestClient, initialized_db: str):
    r = client.post(
        "/deck/nonexistent/notifications",
        data={"enabled": "off"},
        follow_redirects=False,
    )
    assert r.status_code == 404


def test_paused_srs_deck_excluded_from_due_count(client: TestClient, initialized_db: str):
    """A paused deck's due cards must not contribute to the digest
    trigger or body."""
    user = initialized_db
    repo = DeckRepo()
    qrepo = QuestionRepo()
    # Two decks, one card each. Both are due-now (fresh insert).
    deck_a = repo.create(user, "active")
    deck_p = repo.create(user, "paused")
    qrepo.add(user, deck_a, NewQuestion(type=QuestionType.MCQ, prompt="Qa", answer="A"))
    qrepo.add(user, deck_p, NewQuestion(type=QuestionType.MCQ, prompt="Qp", answer="A"))
    # Pause `paused`.
    client.post("/deck/paused/notifications", data={"enabled": "off"}, follow_redirects=False)
    # The digest-counter should now report 1, not 2.
    from prep.study.repo import ReviewRepo

    assert ReviewRepo().count_due_for_user(user) == 1
    # And the breakdown should only include `active`.
    bd = DeckRepo().due_breakdown(user)
    assert [name for name, _ in bd] == ["active"]


def test_trivia_notifications_toggle_now_works_for_srs_via_unified_setter(
    client: TestClient, initialized_db: str
):
    """The repo setter no longer enforces deck_type='trivia' so the
    /trivia/decks/<id>/notifications route accepts any deck. (Kept
    for api compat with the trivia deck UI; new SRS UI uses
    /deck/<name>/notifications.)"""
    deck_id = DeckRepo().create(initialized_db, "regular-srs")
    r = client.post(
        f"/trivia/decks/{deck_id}/notifications",
        data={"enabled": "off"},
        follow_redirects=False,
    )
    assert r.status_code == 303
