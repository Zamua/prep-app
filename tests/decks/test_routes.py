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


def test_index_decks_carry_decktype(client: TestClient, initialized_db: str):
    """The index list also shows the type tag — same slot, same chrome,
    consistent across views."""
    DeckRepo().create(initialized_db, "an-srs-deck")
    DeckRepo().create_trivia(initialized_db, "a-trivia-deck", topic="x", interval_minutes=30)
    r = client.get("/")
    assert r.status_code == 200
    assert "tag-decktype-srs" in r.text
    assert "tag-decktype-trivia" in r.text


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
    from prep import db as _legacy_db

    assert _legacy_db.count_due_for_user(user) == 1
    # And the breakdown should only include `active`.
    bd = _legacy_db.deck_due_breakdown(user)
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
