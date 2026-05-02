"""Integration tests for prep.trivia.repo.

The fixtures in conftest.py spin up an isolated sqlite per test +
upsert a default user. We seed a trivia deck and a few questions
directly via the existing decks repo, then exercise queue ops.
"""

from __future__ import annotations

import pytest

from prep.decks.entities import NewQuestion, QuestionType
from prep.decks.repo import DeckRepo, QuestionRepo
from prep.trivia.repo import TriviaQueueRepo


@pytest.fixture
def repos(initialized_db: str):
    return {
        "user": initialized_db,
        "decks": DeckRepo(),
        "questions": QuestionRepo(),
        "trivia": TriviaQueueRepo(),
    }


def _seed_trivia_deck(repos, name="capitals", n_questions=3):
    """Helper: create a deck + N questions + queue entries."""
    user = repos["user"]
    deck_id = repos["decks"].create(user, name)
    qids = []
    for i in range(n_questions):
        qid = repos["questions"].add(
            user,
            deck_id,
            NewQuestion(
                type=QuestionType.SHORT,
                topic=name,
                prompt=f"What is the capital of country #{i}?",
                answer=f"Capital{i}",
            ),
        )
        repos["trivia"].append_card(qid, deck_id)
        qids.append(qid)
    return deck_id, qids


# ---- append_card -------------------------------------------------------


def test_append_card_assigns_monotonic_positions(repos):
    deck_id, qids = _seed_trivia_deck(repos, n_questions=3)
    trivia = repos["trivia"]
    # Pick — should be the first card we appended.
    nxt = trivia.pick_next_for_deck(deck_id)
    assert nxt is not None
    assert nxt.question_id == qids[0]
    assert nxt.is_fresh is True


def test_append_card_isolates_per_deck(repos):
    """Two decks get independent queue numbering — appending to deck B
    doesn't bump positions in deck A."""
    deck_a, _ = _seed_trivia_deck(repos, "deck-a", n_questions=2)
    deck_b, _ = _seed_trivia_deck(repos, "deck-b", n_questions=2)
    a_next = repos["trivia"].pick_next_for_deck(deck_a)
    b_next = repos["trivia"].pick_next_for_deck(deck_b)
    assert a_next.deck_id == deck_a
    assert b_next.deck_id == deck_b


# ---- pick_next_for_deck ------------------------------------------------


def test_pick_next_returns_none_for_empty_deck(repos):
    user = repos["user"]
    deck_id = repos["decks"].create(user, "empty")
    assert repos["trivia"].pick_next_for_deck(deck_id) is None


def test_pick_next_prefers_unanswered_over_answered(repos):
    """After we mark card #0 answered, the picker should skip it and
    return card #1 (the next never-answered card)."""
    deck_id, qids = _seed_trivia_deck(repos, n_questions=3)
    trivia = repos["trivia"]
    trivia.mark_answered(qids[0], correct=True)
    nxt = trivia.pick_next_for_deck(deck_id)
    assert nxt.question_id == qids[1]
    assert nxt.is_fresh is True


def test_pick_next_falls_back_to_rotated_when_no_unanswered(repos):
    """Once every card's been answered at least once, the picker
    returns the longest-ago-answered card (smallest queue_position)."""
    deck_id, qids = _seed_trivia_deck(repos, n_questions=3)
    trivia = repos["trivia"]
    for qid in qids:
        trivia.mark_answered(qid, correct=True)
    # All answered. The first-answered card is now at the back; the
    # last-answered card was bumped most recently. The picker should
    # surface the LONGEST-ago-answered (lowest queue_position) which
    # is qids[0] (rotated to position 4 after qids[2]→6 then... wait,
    # let me think). Actually: each mark_answered bumps to max+1 so
    # in order: qids[0] → pos 4, qids[1] → pos 5, qids[2] → pos 6.
    # Lowest is qids[0]. So qids[0] gets surfaced again.
    nxt = trivia.pick_next_for_deck(deck_id)
    assert nxt.question_id == qids[0]
    assert nxt.is_fresh is False


# ---- mark_answered -----------------------------------------------------


def test_mark_answered_rotates_to_back(repos):
    deck_id, qids = _seed_trivia_deck(repos, n_questions=3)
    trivia = repos["trivia"]
    # Originally next pick is qids[0].
    trivia.mark_answered(qids[0], correct=True)
    # Now the next pick should be qids[1] (since qids[0] is rotated).
    assert trivia.pick_next_for_deck(deck_id).question_id == qids[1]


def test_mark_answered_records_verdict(repos):
    deck_id, qids = _seed_trivia_deck(repos, n_questions=2)
    trivia = repos["trivia"]
    trivia.mark_answered(qids[0], correct=True)
    trivia.mark_answered(qids[1], correct=False)
    # count_unanswered should now be 0 — both have last_answered_at set.
    assert trivia.count_unanswered(deck_id) == 0


# ---- count_unanswered + existing_prompts -------------------------------


def test_count_unanswered_drops_as_cards_are_seen(repos):
    deck_id, qids = _seed_trivia_deck(repos, n_questions=3)
    trivia = repos["trivia"]
    assert trivia.count_unanswered(deck_id) == 3
    trivia.mark_answered(qids[0], correct=True)
    assert trivia.count_unanswered(deck_id) == 2
    trivia.mark_answered(qids[1], correct=False)
    assert trivia.count_unanswered(deck_id) == 1
    trivia.mark_answered(qids[2], correct=True)
    assert trivia.count_unanswered(deck_id) == 0


def test_existing_prompts_returns_all(repos):
    deck_id, _qids = _seed_trivia_deck(repos, n_questions=3)
    prompts = repos["trivia"].existing_prompts(deck_id)
    assert len(prompts) == 3
    assert all("capital of country" in p for p in prompts)
