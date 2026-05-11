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
    omit the Begin button entirely. The deck-type eyebrow in the
    header communicates the type."""
    DeckRepo().create_trivia(initialized_db, "geo", topic="capitals", interval_minutes=30)
    r = client.get("/deck/geo")
    assert r.status_code == 200
    assert "Begin study session" not in r.text
    assert "deck-type-eyebrow" in r.text


def test_deck_view_shows_decktype_eyebrow_for_srs(client: TestClient, initialized_db: str):
    DeckRepo().create(initialized_db, "regular-srs")
    r = client.get("/deck/regular-srs")
    assert r.status_code == 200
    assert "deck-type-eyebrow" in r.text


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
    assert "mastery-bar" in r.text
    assert "1 of 4 mastered" in r.text
    assert "25%" in r.text  # 1/4 → 25%
    assert "2 unanswered" in r.text
    assert "1 wrong" in r.text


def test_deck_view_renders_add_and_split_pills(client: TestClient, initialized_db: str):
    """Deck-action pills mirror the index page pattern — each action gets
    its own pill. "add" is a direct link to /question/new (no panel
    toggle); "edit with claude" only renders when an agent is configured;
    "split" is a direct link too. There's no longer a single "edit" pill
    that hides both manual + AI behind one toggle."""
    DeckRepo().create(initialized_db, "go-systems")
    r = client.get("/deck/go-systems")
    assert r.status_code == 200
    assert "edit-pill" in r.text
    # "add" pill links straight to the question-new form.
    assert "/deck/go-systems/question/new" in r.text


def test_trivia_deck_edit_panel_shows_topic_editor(client: TestClient, initialized_db: str):
    """Trivia decks: when an agent is configured, the claude-edit panel
    carries the topic-prompt editor (drives future batch generation) in
    addition to the per-card transform. The "add" pill (manual card add)
    is its own separate action, not in the panel."""
    DeckRepo().create_trivia(
        initialized_db, "geo-trivia", topic="capital cities of europe", interval_minutes=30
    )
    r = client.get("/deck/geo-trivia")
    assert r.status_code == 200
    # Manual add-card is reachable via its own pill.
    assert "/deck/geo-trivia/question/new" in r.text
    # Topic editor is also rendered when agent_available — the test
    # client's default agent state may vary; we just check it by markup.
    assert 'name="context_prompt"' in r.text
    assert "capital cities of europe" in r.text
    assert "/deck/geo-trivia/topic" in r.text


def test_manual_add_to_trivia_deck_enters_queue(client: TestClient, initialized_db: str):
    """A card added by hand to a trivia deck must end up in the
    trivia_queue rotation (otherwise the notification scheduler
    can't pick it). add_question's deck-type branch handles this."""
    from prep.trivia.repo import TriviaQueueRepo

    DeckRepo().create_trivia(initialized_db, "geo-trivia", topic="capitals", interval_minutes=30)
    r = client.post(
        "/deck/geo-trivia/question/new",
        data={
            "type": "short",
            "topic": "capitals",
            "prompt": "Capital of France?",
            "answer": "Paris",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    # The card lands in the trivia rotation.
    deck_id = DeckRepo().find_id(initialized_db, "geo-trivia")
    nxt = TriviaQueueRepo().pick_next_for_deck(deck_id)
    assert nxt is not None
    assert nxt.prompt == "Capital of France?"


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


# ---- /deck/<name>/split (manual deck split) ---------------------------


def test_split_form_renders_card_list(client: TestClient, initialized_db: str):
    """GET on the split route shows each card with a checkbox so the
    user can select which to move."""
    user = initialized_db
    deck_id = DeckRepo().create(user, "distributed-systems")
    qrepo = QuestionRepo()
    qrepo.add(
        user,
        deck_id,
        NewQuestion(
            type=QuestionType.SHORT,
            prompt="Two Generals problem proves what?",
            answer="impossible",
        ),
    )
    qrepo.add(
        user,
        deck_id,
        NewQuestion(
            type=QuestionType.SHORT,
            prompt="HTTP method for partial update?",
            answer="patch",
        ),
    )
    r = client.get("/deck/distributed-systems/split")
    assert r.status_code == 200
    assert "Two Generals problem proves what?" in r.text
    assert "HTTP method for partial update?" in r.text
    assert 'name="question_ids"' in r.text


def test_split_srs_deck_moves_selected_cards_into_new_deck(client: TestClient, initialized_db: str):
    """Manual split: cards belonging to the selected ids end up in
    the new deck; un-selected stay in source."""
    user = initialized_db
    deck_id = DeckRepo().create(user, "distributed-systems")
    qrepo = QuestionRepo()
    q_keep = qrepo.add(
        user, deck_id, NewQuestion(type=QuestionType.SHORT, prompt="Keep me", answer="A")
    )
    q_move = qrepo.add(
        user, deck_id, NewQuestion(type=QuestionType.SHORT, prompt="Move me", answer="B")
    )

    r = client.post(
        "/deck/distributed-systems/split",
        data={"new_name": "replication", "question_ids": [str(q_move)]},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/deck/replication" in r.headers["location"]

    # Source has only the unselected card.
    src_id = DeckRepo().find_id(user, "distributed-systems")
    src_cards = qrepo.list_in_deck(user, src_id)
    assert [c.id for c in src_cards] == [q_keep]

    # Destination has only the moved card.
    dst_id = DeckRepo().find_id(user, "replication")
    assert dst_id is not None
    dst_cards = qrepo.list_in_deck(user, dst_id)
    assert [c.id for c in dst_cards] == [q_move]


def test_split_trivia_deck_inherits_topic_and_creates_trivia_type(
    client: TestClient, initialized_db: str
):
    """Splitting a trivia deck with no override new_topic inherits
    the source's context_prompt + creates a trivia-typed dest deck
    so the new deck is notification-driven from day one."""
    user = initialized_db
    deck_id = DeckRepo().create_trivia(
        user, "distributed-systems", topic="databases", interval_minutes=45
    )
    qrepo = QuestionRepo()
    qid = qrepo.add(user, deck_id, NewQuestion(type=QuestionType.SHORT, prompt="?", answer="A"))

    r = client.post(
        "/deck/distributed-systems/split",
        data={"new_name": "replication", "question_ids": [str(qid)]},
        follow_redirects=False,
    )
    assert r.status_code == 303
    new_id = DeckRepo().find_id(user, "replication")
    assert new_id is not None
    assert DeckRepo().get_type(user, new_id).value == "trivia"
    # Topic inherited from source when no override given.
    assert DeckRepo().get_context_prompt(user, "replication") == "databases"


def test_split_trivia_deck_uses_provided_topic_override(client: TestClient, initialized_db: str):
    user = initialized_db
    deck_id = DeckRepo().create_trivia(
        user, "distributed-systems", topic="databases", interval_minutes=30
    )
    qid = QuestionRepo().add(
        user, deck_id, NewQuestion(type=QuestionType.SHORT, prompt="?", answer="A")
    )
    r = client.post(
        "/deck/distributed-systems/split",
        data={
            "new_name": "replication",
            "question_ids": [str(qid)],
            "new_topic": "leader election + linearizability",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert DeckRepo().get_context_prompt(user, "replication") == "leader election + linearizability"


def test_split_rejects_duplicate_new_name(client: TestClient, initialized_db: str):
    user = initialized_db
    src = DeckRepo().create(user, "src")
    DeckRepo().create(user, "already-exists")
    qid = QuestionRepo().add(user, src, NewQuestion(type=QuestionType.MCQ, prompt="?", answer="A"))
    r = client.post(
        "/deck/src/split",
        data={"new_name": "already-exists", "question_ids": [str(qid)]},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "already exists" in r.text


def test_split_rejects_empty_selection(client: TestClient, initialized_db: str):
    user = initialized_db
    deck_id = DeckRepo().create(user, "src")
    QuestionRepo().add(user, deck_id, NewQuestion(type=QuestionType.MCQ, prompt="?", answer="A"))
    r = client.post(
        "/deck/src/split",
        data={"new_name": "dest", "question_ids": []},
        follow_redirects=False,
    )
    assert r.status_code == 400
    # Source deck is intact, no orphan dest deck.
    assert DeckRepo().find_id(user, "dest") is None


def test_split_404s_for_unknown_source_deck(client: TestClient, initialized_db: str):
    r = client.post(
        "/deck/no-such-deck/split",
        data={"new_name": "anything", "question_ids": ["1"]},
        follow_redirects=False,
    )
    assert r.status_code == 404


def test_index_decks_carry_decktype(client: TestClient, initialized_db: str):
    """The index list shows the deck type as a small caps eyebrow above
    the deck name — consistent with the deck-page header. Read-only
    metadata, separated from the row of actionable pills."""
    DeckRepo().create(initialized_db, "an-srs-deck")
    DeckRepo().create_trivia(initialized_db, "a-trivia-deck", topic="x", interval_minutes=30)
    r = client.get("/")
    assert r.status_code == 200
    assert "deck-type-eyebrow-srs" in r.text
    assert "deck-type-eyebrow-trivia" in r.text


def test_index_trivia_deck_shows_mastery_mini_bar(client: TestClient, initialized_db: str):
    """Trivia decks render the mini mastery bar instead of the SRS
    "n due now · m total" line, since trivia has no SRS schedule.
    Bar reflects mastered + wrong + unanswered counts."""
    from prep.decks.entities import NewQuestion, QuestionType
    from prep.decks.repo import QuestionRepo
    from prep.trivia.repo import TriviaQueueRepo

    deck_id = DeckRepo().create_trivia(
        initialized_db, "distributed-systems", topic="x", interval_minutes=30
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


def test_index_renders_continue_strip_when_active_trivia_sessions(
    client: TestClient, initialized_db: str
):
    """If the user has an active trivia session, the index page
    shows a 'Continue trivia' strip with the deck name + remaining
    count, linked to the resume URL."""
    from prep.trivia.repo import TriviaSessionsRepo

    deck_id = DeckRepo().create_trivia(
        initialized_db, "resumable-deck", topic="x", interval_minutes=30
    )
    TriviaSessionsRepo().start_or_resume(
        initialized_db,
        deck_id,
        queue=[101, 102, 103],
        done=[(99, "r")],
    )
    r = client.get("/")
    assert r.status_code == 200
    assert "Continue trivia" in r.text
    assert "3 of 4 cards left" in r.text
    assert "/trivia/session/resumable-deck?cards=101,102,103" in r.text
    assert "done=99r" in r.text


def test_index_omits_continue_strip_when_no_active_trivia_sessions(
    client: TestClient, initialized_db: str
):
    """No active sessions → no Continue strip rendered."""
    DeckRepo().create_trivia(initialized_db, "geo", topic="x", interval_minutes=30)
    r = client.get("/")
    assert r.status_code == 200
    assert "Continue trivia" not in r.text


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


# ---- htmx fragment endpoints -------------------------------------------
#
# The fragment routes return a slice of HTML that swaps in place via
# hx-swap=outerHTML. The contract the JS depends on: the root element
# carries `hx-trigger="every Xs"` ONLY while the workflow is still
# mid-flow. Once a workflow reaches a terminal-from-the-UI-perspective
# state (awaiting_apply for transform, awaiting_feedback for plan, etc.)
# the trigger is omitted and htmx stops polling — the server is the
# loop's owner.
#
# Tests below stub `prep.temporal_client.get_*_progress` + describe_workflow
# with async fakes so we don't need a real Temporal devserver.


def _afake(value):
    """Async fake — coroutine factory that ignores args and resolves to
    `value`. Mirrors the helper in tests/trivia/test_routes.py."""

    async def fn(*_a, **_kw):
        return value

    return fn


# ---- /transform/{wid}/fragment ----------------------------------------


def _seed_transform_wid(initialized_db: str, deck_name: str = "go-systems") -> tuple[int, str]:
    """Returns (deck_id, wid). Workflow id encodes scope='deck' +
    deck_id so the route's `_require_owns_transform` finds the deck."""
    deck_id = DeckRepo().create(initialized_db, deck_name)
    wid = f"transform-deck-{deck_id}-abc1234567"
    return deck_id, wid


def test_transform_fragment_mid_flow_keeps_polling(
    monkeypatch, client: TestClient, initialized_db: str
):
    """Non-terminal status (computing): partial carries hx-trigger so
    htmx keeps polling, and the status text is reflected in the body."""
    from prep import temporal_client

    _, wid = _seed_transform_wid(initialized_db)
    monkeypatch.setattr(temporal_client, "get_transform_progress", _afake({"status": "computing"}))
    monkeypatch.setattr(temporal_client, "describe_workflow", _afake({"status": "RUNNING"}))

    r = client.get(f"/transform/{wid}/fragment")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    # hx-trigger means the fragment will continue polling.
    assert 'hx-trigger="every' in r.text
    # Headline reflects the computing state.
    assert "Thinking" in r.text


def test_transform_fragment_terminal_stops_polling(
    monkeypatch, client: TestClient, initialized_db: str
):
    """awaiting_apply: workflow paused waiting for the user. The partial
    omits hx-trigger AND renders the Apply / Reject buttons."""
    from prep import temporal_client

    _, wid = _seed_transform_wid(initialized_db)
    monkeypatch.setattr(
        temporal_client,
        "get_transform_progress",
        _afake(
            {
                "status": "awaiting_apply",
                "plan": {"modifications": [], "additions": [], "deletions": []},
            }
        ),
    )
    monkeypatch.setattr(temporal_client, "describe_workflow", _afake({"status": "RUNNING"}))

    r = client.get(f"/transform/{wid}/fragment")
    assert r.status_code == 200
    assert 'hx-trigger="every' not in r.text
    # Terminal-state UI elements appear.
    assert "Apply changes" in r.text
    assert "Reject" in r.text


def test_transform_fragment_idor_other_user_404(
    monkeypatch, client: TestClient, initialized_db: str
):
    """A deck belonging to bob → alice (test client) gets 404 from the
    ownership gate. Same shape as not-found, no leak."""
    from prep.auth.repo import UserRepo
    from prep import temporal_client

    UserRepo().upsert("bob@example.com")
    bob_deck_id = DeckRepo().create("bob@example.com", "bobs-deck")
    wid = f"transform-deck-{bob_deck_id}-abc1234567"
    # Even with stubbed temporal, the gate fires first.
    monkeypatch.setattr(temporal_client, "get_transform_progress", _afake({"status": "computing"}))
    monkeypatch.setattr(temporal_client, "describe_workflow", _afake({"status": "RUNNING"}))

    r = client.get(f"/transform/{wid}/fragment")
    assert r.status_code == 404


def test_transform_fragment_is_html_not_json(monkeypatch, client: TestClient, initialized_db: str):
    """Future-refactor guard: the fragment endpoint MUST stay HTML.
    The /status JSON sibling is the legacy endpoint."""
    from prep import temporal_client

    _, wid = _seed_transform_wid(initialized_db)
    monkeypatch.setattr(temporal_client, "get_transform_progress", _afake({"status": "computing"}))
    monkeypatch.setattr(temporal_client, "describe_workflow", _afake({"status": "RUNNING"}))

    r = client.get(f"/transform/{wid}/fragment")
    assert r.status_code == 200
    assert not r.headers["content-type"].startswith("application/json")


# ---- /plan/{wid}/fragment ---------------------------------------------


def _seed_plan_wid(initialized_db: str, deck_name: str = "go-systems") -> str:
    """Plan workflow ids encode the deck name. The route's owner-check
    just resolves the deck name → deck_id under the current user."""
    DeckRepo().create(initialized_db, deck_name)
    return f"plan-{deck_name}-abc1234567"


def test_plan_fragment_mid_flow_keeps_polling(monkeypatch, client: TestClient, initialized_db: str):
    """status=planning is a polling state → hx-trigger present, headline
    shows the in-flight verb."""
    from prep import temporal_client

    wid = _seed_plan_wid(initialized_db)
    monkeypatch.setattr(
        temporal_client,
        "get_plan_progress",
        _afake({"status": "planning", "plan": [], "total": 0, "generated_count": 0, "round": 1}),
    )

    r = client.get(f"/plan/{wid}/fragment")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert 'hx-trigger="every' in r.text
    assert "Planning" in r.text


def test_plan_fragment_terminal_awaiting_feedback_stops_polling(
    monkeypatch, client: TestClient, initialized_db: str
):
    """awaiting_feedback → no hx-trigger; the accept/refine UI renders."""
    from prep import temporal_client

    wid = _seed_plan_wid(initialized_db)
    monkeypatch.setattr(
        temporal_client,
        "get_plan_progress",
        _afake(
            {
                "status": "awaiting_feedback",
                "plan": [{"title": "Goroutines basics", "brief": "what go's scheduler does"}],
                "total": 1,
                "generated_count": 0,
                "round": 1,
            }
        ),
    )

    r = client.get(f"/plan/{wid}/fragment")
    assert r.status_code == 200
    assert 'hx-trigger="every' not in r.text
    # Accept / refine UI markers.
    assert "Accept" in r.text
    assert "Send feedback" in r.text


def test_plan_fragment_idor_other_user_404(monkeypatch, client: TestClient, initialized_db: str):
    from prep.auth.repo import UserRepo
    from prep import temporal_client

    UserRepo().upsert("bob@example.com")
    DeckRepo().create("bob@example.com", "bobs-deck")
    wid = "plan-bobs-deck-abc1234567"
    monkeypatch.setattr(temporal_client, "get_plan_progress", _afake({"status": "planning"}))

    r = client.get(f"/plan/{wid}/fragment")
    assert r.status_code == 404


def test_plan_fragment_is_html_not_json(monkeypatch, client: TestClient, initialized_db: str):
    from prep import temporal_client

    wid = _seed_plan_wid(initialized_db)
    monkeypatch.setattr(
        temporal_client,
        "get_plan_progress",
        _afake({"status": "planning", "plan": [], "total": 0, "generated_count": 0, "round": 1}),
    )

    r = client.get(f"/plan/{wid}/fragment")
    assert r.status_code == 200
    assert not r.headers["content-type"].startswith("application/json")


# ---- end-to-end transform progression ---------------------------------


def test_transform_polling_progression(monkeypatch, client: TestClient, initialized_db: str):
    """Drive the fragment route through two states in sequence:
    first computing (polling continues), then awaiting_apply (polling
    stops + the accept UI renders). Validates the server-driven loop
    lifecycle: the client is dumb, the partial drives the loop."""
    from prep import temporal_client

    _, wid = _seed_transform_wid(initialized_db, "concurrency")
    # State machine the fake walks through. Each call to the progress
    # query pops the next reply.
    states = [
        {"status": "computing"},
        {
            "status": "awaiting_apply",
            "plan": {"modifications": [], "additions": [], "deletions": []},
        },
    ]
    calls = {"n": 0}

    async def fake_progress(_wid):
        i = min(calls["n"], len(states) - 1)
        calls["n"] += 1
        return states[i]

    monkeypatch.setattr(temporal_client, "get_transform_progress", fake_progress)
    monkeypatch.setattr(temporal_client, "describe_workflow", _afake({"status": "RUNNING"}))

    # First poll: still computing → polling continues.
    r1 = client.get(f"/transform/{wid}/fragment")
    assert r1.status_code == 200
    assert 'hx-trigger="every' in r1.text
    assert "Thinking" in r1.text

    # Second poll: awaiting_apply → polling stops + accept UI shows.
    r2 = client.get(f"/transform/{wid}/fragment")
    assert r2.status_code == 200
    assert 'hx-trigger="every' not in r2.text
    assert "Apply changes" in r2.text
    assert calls["n"] == 2


# ---- POST apply/reject return fragments, never block -------------------
#
# These tests pin the post-refactor contract: the apply/reject routes
# send the temporal signal, query the workflow for its fresh status
# (which the new transient `applying`/`rejecting` workflow states make
# meaningful immediately), and return the rendered partial. They MUST
# NOT call get_transform_result / get_grade_result / handle.result(),
# because those long-poll on workflow completion and were the source
# of the 1-5s "hung button" UX bug.


def test_transform_apply_returns_fragment_with_applying_status(
    monkeypatch, client: TestClient, initialized_db: str
):
    """POST /transform/{wid}/apply: signal goes through, the response is
    the partial fragment with status=applying rendered, and the polling
    loop continues (hx-trigger present)."""
    from prep import temporal_client

    _, wid = _seed_transform_wid(initialized_db, "concurrency")
    signals: list[str] = []

    async def fake_signal(_wid):
        signals.append(_wid)

    monkeypatch.setattr(temporal_client, "signal_apply_transform", fake_signal)
    monkeypatch.setattr(
        temporal_client,
        "get_transform_progress",
        _afake({"status": "applying"}),
    )

    r = client.post(f"/transform/{wid}/apply", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    # Signal was sent.
    assert signals == [wid]
    # Fragment has the applying-state UI and continues polling.
    assert "Applying" in r.text
    assert 'hx-trigger="every' in r.text


def test_transform_reject_returns_fragment_with_rejecting_status(
    monkeypatch, client: TestClient, initialized_db: str
):
    """POST /transform/{wid}/reject: same shape — signal + partial
    response. Status is `rejecting` (the new transient state). Polling
    continues briefly so the partial picks up the eventual `rejected`
    or `gone` terminal state without the user refreshing."""
    from prep import temporal_client

    _, wid = _seed_transform_wid(initialized_db, "concurrency")
    signals: list[str] = []

    async def fake_signal(_wid):
        signals.append(_wid)

    monkeypatch.setattr(temporal_client, "signal_reject_transform", fake_signal)
    monkeypatch.setattr(
        temporal_client,
        "get_transform_progress",
        _afake({"status": "rejecting"}),
    )

    r = client.post(f"/transform/{wid}/reject", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert signals == [wid]
    assert "Cancelling" in r.text
    # rejecting is in _polling_states so the partial keeps polling
    # until the workflow flips to rejected/gone.
    assert 'hx-trigger="every' in r.text


def test_transform_apply_does_not_call_get_transform_result(
    monkeypatch, client: TestClient, initialized_db: str
):
    """Sentinel guard: if anyone re-introduces a blocking
    handle.result() call in the apply route, this test trips. We
    monkeypatch get_transform_result to raise an unmistakable error
    if invoked, then exercise the route and assert success."""
    from prep import temporal_client

    _, wid = _seed_transform_wid(initialized_db, "concurrency")

    async def boom(_wid):
        raise AssertionError("get_transform_result must not be called from apply route")

    async def noop_signal(_wid):
        pass

    monkeypatch.setattr(temporal_client, "signal_apply_transform", noop_signal)
    monkeypatch.setattr(temporal_client, "get_transform_result", boom)
    monkeypatch.setattr(
        temporal_client,
        "get_transform_progress",
        _afake({"status": "applying"}),
    )

    r = client.post(f"/transform/{wid}/apply", follow_redirects=False)
    assert r.status_code == 200


def test_transform_reject_does_not_call_get_transform_result(
    monkeypatch, client: TestClient, initialized_db: str
):
    """Same guard for the reject route."""
    from prep import temporal_client

    _, wid = _seed_transform_wid(initialized_db, "concurrency")

    async def boom(_wid):
        raise AssertionError("get_transform_result must not be called from reject route")

    async def noop_signal(_wid):
        pass

    monkeypatch.setattr(temporal_client, "signal_reject_transform", noop_signal)
    monkeypatch.setattr(temporal_client, "get_transform_result", boom)
    monkeypatch.setattr(
        temporal_client,
        "get_transform_progress",
        _afake({"status": "rejecting"}),
    )

    r = client.post(f"/transform/{wid}/reject", follow_redirects=False)
    assert r.status_code == 200


def test_transform_reject_handles_workflow_already_gone(
    monkeypatch, client: TestClient, initialized_db: str
):
    """Edge case: signal arrives, workflow processes + closes faster
    than the route can query. progress=None falls through to a
    describe-status fallback (NOT a blocking result() call). The
    fragment renders with status=gone and the polling stops."""
    from prep import temporal_client

    _, wid = _seed_transform_wid(initialized_db, "concurrency")

    async def noop_signal(_wid):
        pass

    async def boom(_wid):
        raise AssertionError("get_transform_result must not be called when progress is None")

    monkeypatch.setattr(temporal_client, "signal_reject_transform", noop_signal)
    monkeypatch.setattr(temporal_client, "get_transform_result", boom)
    monkeypatch.setattr(temporal_client, "get_transform_progress", _afake(None))
    monkeypatch.setattr(temporal_client, "describe_workflow", _afake({"status": "CANCELED"}))

    r = client.post(f"/transform/{wid}/reject", follow_redirects=False)
    assert r.status_code == 200
    assert "Cancelled" in r.text
    # `gone` is a terminal state from the polling perspective.
    assert 'hx-trigger="every' not in r.text


# ---- POST plan accept/reject/feedback return fragments -----------------


def test_plan_accept_returns_fragment_with_accepting_status(
    monkeypatch, client: TestClient, initialized_db: str
):
    """POST /plan/{wid}/accept: signal sent, fragment returned with
    status=accepting (the new transient state). Polling continues."""
    from prep import temporal_client

    wid = _seed_plan_wid(initialized_db)
    signals: list[str] = []

    async def fake_signal(_wid):
        signals.append(_wid)

    monkeypatch.setattr(temporal_client, "signal_plan_accept", fake_signal)
    monkeypatch.setattr(
        temporal_client,
        "get_plan_progress",
        _afake({"status": "accepting", "plan": [], "total": 0, "generated_count": 0, "round": 1}),
    )

    r = client.post(f"/plan/{wid}/accept", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert signals == [wid]
    assert "Accepting" in r.text
    assert 'hx-trigger="every' in r.text


def test_plan_reject_returns_fragment_with_rejecting_status(
    monkeypatch, client: TestClient, initialized_db: str
):
    """POST /plan/{wid}/reject: signal sent, fragment returned with
    status=rejecting. Polls briefly so the partial picks up the
    rejected/gone terminal state."""
    from prep import temporal_client

    wid = _seed_plan_wid(initialized_db)
    signals: list[str] = []

    async def fake_signal(_wid):
        signals.append(_wid)

    monkeypatch.setattr(temporal_client, "signal_plan_reject", fake_signal)
    monkeypatch.setattr(
        temporal_client,
        "get_plan_progress",
        _afake({"status": "rejecting", "plan": [], "total": 0, "generated_count": 0, "round": 1}),
    )

    r = client.post(f"/plan/{wid}/reject", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert signals == [wid]
    assert "Cancelling" in r.text
    assert 'hx-trigger="every' in r.text


def test_plan_feedback_returns_fragment(monkeypatch, client: TestClient, initialized_db: str):
    """POST /plan/{wid}/feedback: signal sent with the feedback string,
    fragment returned. Server moves the workflow to replanning; the
    response reflects whatever progress query says (here: replanning)
    and continues polling."""
    from prep import temporal_client

    wid = _seed_plan_wid(initialized_db)
    signals: list[tuple[str, str]] = []

    async def fake_signal(_wid, fb):
        signals.append((_wid, fb))

    monkeypatch.setattr(temporal_client, "signal_plan_feedback", fake_signal)
    monkeypatch.setattr(
        temporal_client,
        "get_plan_progress",
        _afake(
            {
                "status": "replanning",
                "plan": [{"title": "old", "brief": "..."}],
                "total": 1,
                "generated_count": 0,
                "round": 2,
            }
        ),
    )

    r = client.post(
        f"/plan/{wid}/feedback",
        data={"feedback": "add 2 more on goroutines"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert signals == [(wid, "add 2 more on goroutines")]
    assert "Refining" in r.text
    assert 'hx-trigger="every' in r.text


def test_plan_accept_idor_other_user_404(monkeypatch, client: TestClient, initialized_db: str):
    """Ownership gate fires before the signal — alice can't accept
    bob's plan even with a guessed wid."""
    from prep.auth.repo import UserRepo
    from prep import temporal_client

    UserRepo().upsert("bob@example.com")
    DeckRepo().create("bob@example.com", "bobs-deck")
    wid = "plan-bobs-deck-abc1234567"

    called = {"signal": False}

    async def fake_signal(_wid):
        called["signal"] = True

    monkeypatch.setattr(temporal_client, "signal_plan_accept", fake_signal)

    r = client.post(f"/plan/{wid}/accept", follow_redirects=False)
    assert r.status_code == 404
    # Critical: no signal sent — owner check gates upstream of the
    # temporal call so cross-user pokes don't even touch the workflow.
    assert called["signal"] is False
