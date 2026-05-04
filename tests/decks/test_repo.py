"""Integration tests for prep.decks.repo against a real sqlite.

The fixtures in conftest.py give each test its own temp-path sqlite
+ run init() against it, so these are isolated and fast."""

from __future__ import annotations

import pytest

from prep.decks.entities import (
    DeckCard,
    DeckSummary,
    NewQuestion,
    Question,
    QuestionType,
)
from prep.decks.repo import DeckRepo, QuestionRepo


@pytest.fixture
def deck_repo(initialized_db: str) -> DeckRepo:
    return DeckRepo()


@pytest.fixture
def q_repo(initialized_db: str) -> QuestionRepo:
    return QuestionRepo()


# ---- DeckRepo ----------------------------------------------------------


def test_create_then_find(deck_repo: DeckRepo, initialized_db: str):
    user = initialized_db
    deck_id = deck_repo.create(user, "go-systems")
    assert deck_id > 0
    assert deck_repo.find_id(user, "go-systems") == deck_id
    assert deck_repo.find_id(user, "nonexistent") is None


def test_get_or_create_is_idempotent(deck_repo: DeckRepo, initialized_db: str):
    user = initialized_db
    a = deck_repo.get_or_create(user, "decks-101")
    b = deck_repo.get_or_create(user, "decks-101")
    assert a == b


def test_create_with_context_prompt(deck_repo: DeckRepo, initialized_db: str):
    user = initialized_db
    deck_repo.create(user, "go-systems", context_prompt="Concurrency primitives in Go.")
    cp = deck_repo.get_context_prompt(user, "go-systems")
    assert cp == "Concurrency primitives in Go."


def test_update_context_prompt(deck_repo: DeckRepo, initialized_db: str):
    user = initialized_db
    deck_repo.create(user, "go-systems")
    deck_repo.update_context_prompt(user, "go-systems", "Fresh prompt.")
    assert deck_repo.get_context_prompt(user, "go-systems") == "Fresh prompt."


def test_list_summaries_includes_counts(
    deck_repo: DeckRepo, q_repo: QuestionRepo, initialized_db: str
):
    user = initialized_db
    deck_id = deck_repo.create(user, "go-systems")
    q_repo.add(user, deck_id, NewQuestion(type=QuestionType.MCQ, prompt="?", answer="42"))
    q_repo.add(user, deck_id, NewQuestion(type=QuestionType.MCQ, prompt="??", answer="43"))

    summaries = deck_repo.list_summaries(user)
    assert len(summaries) == 1
    assert all(isinstance(s, DeckSummary) for s in summaries)
    assert summaries[0].name == "go-systems"
    assert summaries[0].total == 2
    # Newly-inserted cards land with next_due = ts (now-at-insert), so
    # the list_decks query (which compares next_due <= now-at-query) sees
    # them as due. Pinning that behavior — anyone tweaking the insert
    # path or the due query should notice this assertion change.
    assert summaries[0].due == 2


def test_delete_removes_deck_and_questions(
    deck_repo: DeckRepo, q_repo: QuestionRepo, initialized_db: str
):
    user = initialized_db
    deck_id = deck_repo.create(user, "doomed")
    qid = q_repo.add(user, deck_id, NewQuestion(type=QuestionType.MCQ, prompt="?", answer="x"))
    deck_repo.delete(user, "doomed")
    assert deck_repo.find_id(user, "doomed") is None
    # FK CASCADE: question is gone too.
    assert q_repo.get(user, qid) is None


def test_user_isolation_on_find(deck_repo: DeckRepo, initialized_db: str):
    """Two users with same deck name don't see each other's decks."""
    alice = initialized_db
    deck_repo.create(alice, "shared-name")

    # Create a second user and a deck under their name.
    from prep.auth.repo import UserRepo

    UserRepo().upsert("bob@example.com", display_name="Bob")
    bob_deck = deck_repo.create("bob@example.com", "shared-name")

    alice_deck = deck_repo.find_id(alice, "shared-name")
    assert alice_deck is not None
    assert alice_deck != bob_deck


# ---- QuestionRepo ------------------------------------------------------


def test_add_question_returns_id(deck_repo: DeckRepo, q_repo: QuestionRepo, initialized_db: str):
    user = initialized_db
    deck_id = deck_repo.create(user, "go-systems")
    qid = q_repo.add(
        user,
        deck_id,
        NewQuestion(type=QuestionType.MCQ, prompt="?", answer="A", choices=["A", "B"]),
    )
    assert qid > 0


def test_get_question_returns_entity(
    deck_repo: DeckRepo, q_repo: QuestionRepo, initialized_db: str
):
    user = initialized_db
    deck_id = deck_repo.create(user, "go-systems")
    qid = q_repo.add(
        user,
        deck_id,
        NewQuestion(
            type=QuestionType.MCQ,
            prompt="When use mutex?",
            answer="A",
            choices=["A", "B"],
            topic="concurrency",
        ),
    )
    q = q_repo.get(user, qid)
    assert isinstance(q, Question)
    assert q is not None
    assert q.type is QuestionType.MCQ
    assert q.prompt == "When use mutex?"
    assert q.choices == ["A", "B"]
    assert q.topic == "concurrency"


def test_get_question_user_isolation(
    deck_repo: DeckRepo, q_repo: QuestionRepo, initialized_db: str
):
    """A repo.get() with the wrong user_id returns None — no IDOR."""
    alice = initialized_db
    from prep.auth.repo import UserRepo

    UserRepo().upsert("bob@example.com")

    deck_id = deck_repo.create(alice, "alices-deck")
    qid = q_repo.add(alice, deck_id, NewQuestion(type=QuestionType.MCQ, prompt="?", answer="A"))
    assert q_repo.get(alice, qid) is not None
    assert q_repo.get("bob@example.com", qid) is None


def test_update_question_preserves_id(
    deck_repo: DeckRepo, q_repo: QuestionRepo, initialized_db: str
):
    user = initialized_db
    deck_id = deck_repo.create(user, "go-systems")
    qid = q_repo.add(user, deck_id, NewQuestion(type=QuestionType.MCQ, prompt="old", answer="A"))
    q_repo.update(
        user,
        qid,
        NewQuestion(type=QuestionType.SHORT, prompt="new", answer="rewritten"),
    )
    after = q_repo.get(user, qid)
    assert after is not None
    assert after.id == qid
    assert after.type is QuestionType.SHORT
    assert after.prompt == "new"


def test_list_in_deck_returns_deck_cards(
    deck_repo: DeckRepo, q_repo: QuestionRepo, initialized_db: str
):
    """list_in_deck returns DeckCard (question display + SRS state),
    not full Question — the deck view doesn't need user_id/created_at
    so we omit them at the type level."""
    user = initialized_db
    deck_id = deck_repo.create(user, "go-systems")
    q_repo.add(user, deck_id, NewQuestion(type=QuestionType.MCQ, prompt="q1", answer="A"))
    q_repo.add(user, deck_id, NewQuestion(type=QuestionType.MCQ, prompt="q2", answer="B"))
    cards = q_repo.list_in_deck(user, deck_id)
    assert len(cards) == 2
    assert all(isinstance(c, DeckCard) for c in cards)
    # SRS fields are populated even at step 0.
    assert cards[0].step == 0
    assert cards[0].next_due
    assert cards[0].attempts == 0


def test_set_suspended_flips_flag(deck_repo: DeckRepo, q_repo: QuestionRepo, initialized_db: str):
    user = initialized_db
    deck_id = deck_repo.create(user, "go-systems")
    qid = q_repo.add(user, deck_id, NewQuestion(type=QuestionType.MCQ, prompt="?", answer="A"))
    assert q_repo.get(user, qid).suspended is False
    q_repo.set_suspended(user, qid, True)
    assert q_repo.get(user, qid).suspended is True
    q_repo.set_suspended(user, qid, False)
    assert q_repo.get(user, qid).suspended is False
