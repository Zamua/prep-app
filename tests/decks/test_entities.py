"""Tests for prep.decks.entities — pydantic validation on the
deck + question models. Pure unit tests, no DB."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from prep.decks.entities import (
    Deck,
    DeckCard,
    DeckSummary,
    NewQuestion,
    Question,
    QuestionType,
)

# ---- Deck --------------------------------------------------------------


def test_deck_minimal_fields():
    d = Deck(
        id=1, user_id="alice@example.com", name="go-systems", created_at="2026-04-29T12:00:00Z"
    )
    assert d.id == 1
    assert d.context_prompt is None


def test_deck_with_context_prompt():
    d = Deck(
        id=1,
        user_id="alice@example.com",
        name="go-systems",
        created_at="2026-04-29T12:00:00Z",
        context_prompt="The user wants to learn Go concurrency primitives.",
    )
    assert d.context_prompt is not None


def test_deck_rejects_empty_name():
    with pytest.raises(ValidationError):
        Deck(id=1, user_id="alice", name="", created_at="2026-04-29T12:00:00Z")


def test_deck_rejects_overlong_name():
    with pytest.raises(ValidationError):
        Deck(
            id=1,
            user_id="alice",
            name="x" * 201,
            created_at="2026-04-29T12:00:00Z",
        )


# ---- DeckSummary -------------------------------------------------------


def test_deck_summary_shape():
    s = DeckSummary(id=1, name="go-systems", total=12, due=5)
    assert s.total == 12
    assert s.due == 5


def test_deck_summary_round_trips_dict():
    """Repos return summaries from sqlite; routes JSON-encode them.
    Validate dict → entity → dict is identity (modulo defaults)."""
    raw = {"id": 1, "name": "go-systems", "total": 12, "due": 5}
    s = DeckSummary.model_validate(raw)
    assert s.model_dump() == raw


# ---- QuestionType ------------------------------------------------------


def test_question_type_serializes_as_string():
    assert QuestionType.MCQ == "mcq"
    assert QuestionType.MULTI == "multi"
    assert QuestionType.CODE == "code"
    assert QuestionType.SHORT == "short"


def test_question_type_rejects_unknown_kind():
    with pytest.raises(ValueError):
        QuestionType("javascript")


# ---- Question ----------------------------------------------------------


def _q_kwargs(**overrides) -> dict:
    base = {
        "id": 42,
        "user_id": "alice@example.com",
        "deck_id": 1,
        "type": QuestionType.MCQ,
        "prompt": "What is 2 + 2?",
        "answer": "4",
        "created_at": "2026-04-29T12:00:00Z",
    }
    base.update(overrides)
    return base


def test_question_minimal_fields():
    q = Question(**_q_kwargs())
    assert q.suspended is False
    assert q.choices is None
    assert q.rubric is None


def test_question_with_choices():
    q = Question(**_q_kwargs(type=QuestionType.MCQ, choices=["A", "B", "C", "D"], answer="A"))
    assert q.choices == ["A", "B", "C", "D"]


def test_question_rejects_empty_prompt():
    with pytest.raises(ValidationError):
        Question(**_q_kwargs(prompt=""))


def test_question_accepts_string_type():
    """The repo passes raw row dicts; pydantic should coerce 'mcq' → enum."""
    q = Question.model_validate(_q_kwargs(type="mcq"))
    assert q.type is QuestionType.MCQ


def test_question_round_trips_dict_for_repo():
    """Critical: dict-from-sqlite → entity → dict matches what the repo
    used to return raw. Anything that round-trips this way means
    callers can switch to entities without touching their templates
    or downstream consumers."""
    raw = {
        "id": 42,
        "user_id": "alice@example.com",
        "deck_id": 1,
        "type": "mcq",
        "topic": "system-design",
        "prompt": "When should you choose strong consistency?",
        "choices": ["A", "B", "C"],
        "answer": "A",
        "rubric": None,
        "created_at": "2026-04-29T12:00:00Z",
        "suspended": False,
        "skeleton": None,
        "language": None,
    }
    q = Question.model_validate(raw)
    out = q.model_dump()
    # `type` enum dumps as string by default — that matches the raw form.
    assert out == raw


# ---- DeckCard ----------------------------------------------------------


def _card_kwargs(**overrides) -> dict:
    """DeckCard-specific kwargs (no user_id/deck_id/created_at — by
    design, since the route already knows that context)."""
    base = {
        "id": 42,
        "type": QuestionType.MCQ,
        "prompt": "What is 2 + 2?",
        "answer": "4",
        "next_due": "2026-04-30T12:00:00Z",
    }
    base.update(overrides)
    return base


def test_deck_card_carries_srs_state():
    c = DeckCard(**_card_kwargs(step=2, rights=4, attempts=5))
    assert c.step == 2
    assert c.attempts == 5
    assert c.prompt == "What is 2 + 2?"


def test_deck_card_step_defaults_to_zero():
    c = DeckCard(**_card_kwargs())
    assert c.step == 0
    assert c.rights == 0
    assert c.attempts == 0


# ---- NewQuestion -------------------------------------------------------


def test_new_question_excludes_server_fields():
    """NewQuestion is the request shape; it shouldn't accept id/user_id/deck_id."""
    n = NewQuestion(type=QuestionType.MCQ, prompt="?", answer="42")
    # id is not on the model, so model_dump() doesn't include it
    assert "id" not in n.model_dump()
    assert "user_id" not in n.model_dump()
    assert "deck_id" not in n.model_dump()


def test_new_question_validates_type():
    with pytest.raises(ValidationError):
        NewQuestion(type="javascript", prompt="?", answer="42")
