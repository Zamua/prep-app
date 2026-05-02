"""Tests for prep.trivia.service.

`generate_batch` is exercised with the agent_client.run_prompt
monkey-patched to return canned stdout — that mirrors the pattern
the decks/tests use to fake out claude.

`grade_answer` is pure, tested directly.
"""

from __future__ import annotations

import pytest

from prep.decks.repo import DeckRepo, QuestionRepo
from prep.trivia import service as svc
from prep.trivia.agent_client import AgentUnavailable
from prep.trivia.repo import TriviaQueueRepo

# ---- generate_batch ----------------------------------------------------


@pytest.fixture
def fixtures(initialized_db: str):
    user = initialized_db
    decks = DeckRepo()
    questions = QuestionRepo()
    trivia = TriviaQueueRepo()
    deck_id = decks.create(user, "world-capitals")
    return {
        "user": user,
        "deck_id": deck_id,
        "decks": decks,
        "questions": questions,
        "trivia": trivia,
    }


def test_generate_batch_inserts_pairs(monkeypatch, fixtures):
    """Happy path — claude returns 3 valid pairs, all 3 land in db."""
    fixed = """[
        {"q": "Capital of France?", "a": "Paris"},
        {"q": "Capital of Japan?", "a": "Tokyo"},
        {"q": "Capital of Egypt?", "a": "Cairo"}
    ]"""
    monkeypatch.setattr(svc, "run_prompt", lambda _p: fixed)
    out = svc.generate_batch(
        user_id=fixtures["user"],
        deck_id=fixtures["deck_id"],
        topic="world capitals",
        questions_repo=fixtures["questions"],
        trivia_repo=fixtures["trivia"],
        batch_size=3,
    )
    assert out.inserted == 3
    assert out.skipped_duplicates == 0
    assert out.skipped_invalid == 0


def test_generate_batch_strips_code_fences(monkeypatch, fixtures):
    """Claude sometimes wraps JSON in ```json — we tolerate it."""
    fixed = '```json\n[{"q": "x?", "a": "y"}]\n```'
    monkeypatch.setattr(svc, "run_prompt", lambda _p: fixed)
    out = svc.generate_batch(
        user_id=fixtures["user"],
        deck_id=fixtures["deck_id"],
        topic="t",
        questions_repo=fixtures["questions"],
        trivia_repo=fixtures["trivia"],
        batch_size=1,
    )
    assert out.inserted == 1


def test_generate_batch_skips_dupes(monkeypatch, fixtures):
    """Re-running with the same prompts → all duplicates, none inserted."""
    fixed = '[{"q": "Capital of France?", "a": "Paris"}]'
    monkeypatch.setattr(svc, "run_prompt", lambda _p: fixed)
    svc.generate_batch(
        user_id=fixtures["user"],
        deck_id=fixtures["deck_id"],
        topic="t",
        questions_repo=fixtures["questions"],
        trivia_repo=fixtures["trivia"],
        batch_size=1,
    )
    out = svc.generate_batch(
        user_id=fixtures["user"],
        deck_id=fixtures["deck_id"],
        topic="t",
        questions_repo=fixtures["questions"],
        trivia_repo=fixtures["trivia"],
        batch_size=1,
    )
    assert out.inserted == 0
    assert out.skipped_duplicates == 1


def test_generate_batch_skips_invalid_entries(monkeypatch, fixtures):
    """Empty Q or empty A → invalid → skipped, not inserted."""
    fixed = """[
        {"q": "", "a": "lol"},
        {"q": "valid?", "a": ""},
        {"q": "ok?", "a": "yes"}
    ]"""
    monkeypatch.setattr(svc, "run_prompt", lambda _p: fixed)
    out = svc.generate_batch(
        user_id=fixtures["user"],
        deck_id=fixtures["deck_id"],
        topic="t",
        questions_repo=fixtures["questions"],
        trivia_repo=fixtures["trivia"],
        batch_size=3,
    )
    assert out.inserted == 1
    assert out.skipped_invalid == 2


def test_generate_batch_raises_on_unparseable(monkeypatch, fixtures):
    """Claude returns prose only → AgentUnavailable, not silent
    write of garbage."""
    monkeypatch.setattr(svc, "run_prompt", lambda _p: "Sorry, I can't help with that.")
    with pytest.raises(AgentUnavailable):
        svc.generate_batch(
            user_id=fixtures["user"],
            deck_id=fixtures["deck_id"],
            topic="t",
            questions_repo=fixtures["questions"],
            trivia_repo=fixtures["trivia"],
            batch_size=1,
        )


# ---- grade_answer ------------------------------------------------------


@pytest.mark.parametrize(
    "expected,given",
    [
        ("Paris", "paris"),
        ("Paris", "PARIS"),
        ("U.S.A.", "usa"),
        ("The Beatles", "the beatles"),
        ("Newton", "Isaac Newton"),  # given includes all expected tokens
        ("Charles Dickens", "Charles  Dickens"),  # whitespace runs collapsed
    ],
)
def test_grade_answer_accepts_equivalents(expected, given):
    assert svc.grade_answer(expected=expected, given=given) is True


@pytest.mark.parametrize(
    "expected,given",
    [
        ("Paris", "London"),
        ("Newton", ""),  # blank
        ("Isaac Newton", "Newton"),  # given missing one of expected tokens
        ("Paris", "  "),
    ],
)
def test_grade_answer_rejects_wrong(expected, given):
    assert svc.grade_answer(expected=expected, given=given) is False
