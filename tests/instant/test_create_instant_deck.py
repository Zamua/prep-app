"""`create_instant_deck`: one transaction that mints the account,
writes the deck and seeds every card, or writes nothing at all."""

from __future__ import annotations

import re
import sqlite3

import pytest

from prep.auth.limits import ANON_MAX_DECKS, ANON_MAX_QUESTIONS, RowCapReached
from prep.decks.entities import SLUG_LENGTH, NewQuestion, QuestionType
from prep.decks.repo import DeckRepo, QuestionRepo
from prep.infrastructure.db import cursor, now
from prep.instant import repo

SLUG_RE = re.compile(r"^[abcdefghijkmnpqrstuvwxyz23456789]{8}$")


def _cards(n: int = 5) -> list[dict]:
    return [
        {"prompt": f"Question {i}?", "answer": f"answer {i}", "answer_regex": f"answer {i}"}
        for i in range(n)
    ]


def _counts() -> dict[str, int]:
    with cursor() as c:
        return {
            table: c.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in ("users", "decks", "questions", "cards")
        }


def _anon_users() -> list[dict]:
    with cursor() as c:
        return [dict(r) for r in c.execute("SELECT * FROM users WHERE is_anonymous = 1")]


def seed_anon_user(external_id: str) -> str:
    ts = now()
    with cursor() as c:
        c.execute(
            """INSERT INTO users (tailscale_login, display_name, email, created_at,
                                  last_seen_at, is_anonymous)
               VALUES (?, 'Guest', NULL, ?, ?, 1)""",
            (external_id, ts, ts),
        )
    return external_id


# ---- the mint ---------------------------------------------------------------


def test_mint_writes_the_account_the_deck_and_every_card(initialized_db: str):
    result = repo.create_instant_deck(user_id=None, display_name="Postgres MVCC", cards=_cards())

    assert result.minted is True
    assert result.user_id.startswith("anon:")
    assert len(result.user_id) == len("anon:") + 32
    assert SLUG_RE.match(result.slug)
    assert len(result.slug) == SLUG_LENGTH

    rows = _anon_users()
    assert len(rows) == 1
    assert rows[0]["tailscale_login"] == result.user_id
    assert rows[0]["display_name"] == "Guest"
    assert rows[0]["email"] is None
    assert rows[0]["is_anonymous"] == 1

    with cursor() as c:
        deck = dict(
            c.execute("SELECT * FROM decks WHERE user_id = ?", (result.user_id,)).fetchone()
        )
        questions = [
            dict(r)
            for r in c.execute(
                "SELECT * FROM questions WHERE user_id = ? ORDER BY id", (result.user_id,)
            )
        ]
        cards = [
            dict(r)
            for r in c.execute(
                "SELECT cards.* FROM cards JOIN questions q ON q.id = cards.question_id"
                " WHERE q.user_id = ? ORDER BY cards.question_id",
                (result.user_id,),
            )
        ]
    assert deck["name"] == result.slug
    assert deck["display_name"] == "Postgres MVCC"
    assert len(questions) == 5
    assert {q["type"] for q in questions} == {"short"}
    assert questions[0]["prompt"] == "Question 0?"
    assert questions[0]["answer_regex"] == "answer 0"
    assert len(cards) == 5
    assert {c["step"] for c in cards} == {0}
    assert all(c["next_due"] == questions[0]["created_at"] for c in cards)


def test_a_second_generation_reuses_the_account(initialized_db: str):
    first = repo.create_instant_deck(user_id=None, display_name="One", cards=_cards(3))
    second = repo.create_instant_deck(user_id=first.user_id, display_name="Two", cards=_cards(3))

    assert second.minted is False
    assert second.user_id == first.user_id
    assert second.slug != first.slug
    assert len(_anon_users()) == 1
    assert _counts()["decks"] == 2


def test_a_signed_in_user_gets_the_deck_and_no_anonymous_row(initialized_db: str):
    result = repo.create_instant_deck(user_id=initialized_db, display_name="Owned", cards=_cards(3))

    assert result.minted is False
    assert result.user_id == initialized_db
    assert _anon_users() == []


# ---- the slug ---------------------------------------------------------------


def test_same_topic_twice_yields_distinct_opaque_slugs(initialized_db: str):
    first = repo.create_instant_deck(user_id=None, display_name="Kafka", cards=_cards(3))
    second = repo.create_instant_deck(user_id=first.user_id, display_name="Kafka", cards=_cards(3))

    assert first.slug != second.slug
    for slug in (first.slug, second.slug):
        assert SLUG_RE.match(slug)
        assert "kafka" not in slug


def test_the_slug_is_never_derived_from_the_display_name(initialized_db: str):
    long_topic = "x" * 500
    first = repo.create_instant_deck(user_id=None, display_name=long_topic, cards=_cards(3))
    second = repo.create_instant_deck(user_id=None, display_name="日本語の歴史", cards=_cards(3))

    for result in (first, second):
        assert SLUG_RE.match(result.slug)


# ---- the row cap ------------------------------------------------------------


def test_an_anonymous_account_is_refused_the_sixth_deck(initialized_db: str):
    user_id = seed_anon_user("anon:" + "ab" * 16)
    for _ in range(ANON_MAX_DECKS):
        repo.create_instant_deck(user_id=user_id, display_name="Deck", cards=_cards(1))
    before = _counts()

    with pytest.raises(RowCapReached):
        repo.create_instant_deck(user_id=user_id, display_name="Sixth", cards=_cards(1))

    assert _counts() == before


def test_the_question_cap_refuses_a_deck_that_would_cross_it(initialized_db: str):
    user_id = seed_anon_user("anon:" + "cd" * 16)
    repo.create_instant_deck(
        user_id=user_id, display_name="Full", cards=_cards(ANON_MAX_QUESTIONS - 2)
    )
    before = _counts()

    with pytest.raises(RowCapReached):
        repo.create_instant_deck(user_id=user_id, display_name="Over", cards=_cards(3))

    assert _counts() == before


def test_a_signed_in_account_has_no_row_cap(initialized_db: str):
    for _ in range(ANON_MAX_DECKS + 1):
        repo.create_instant_deck(user_id=initialized_db, display_name="Deck", cards=_cards(1))

    assert _counts()["decks"] == ANON_MAX_DECKS + 1


# ---- atomicity --------------------------------------------------------------


class _FailingCard(dict):
    """Raises when its regex is read, which every write path does last.
    Two cards are already written by then."""

    def __getitem__(self, key):
        if key == "answer_regex":
            raise RuntimeError("injected failure")
        return super().__getitem__(key)


def _cards_failing_at_the_third() -> list[dict]:
    cards = _cards(5)
    cards[2] = _FailingCard(cards[2])
    return cards


def test_a_failure_part_way_leaves_no_user_no_deck_no_cards(initialized_db: str):
    before = _counts()

    with pytest.raises(RuntimeError):
        repo.create_instant_deck(
            user_id=None, display_name="Torn", cards=_cards_failing_at_the_third()
        )

    assert _counts() == before
    assert _anon_users() == []


def test_a_failure_inside_a_card_insert_rolls_the_whole_deck_back(initialized_db: str):
    cards = _cards(5)
    cards[2]["answer_regex"] = object()  # unbindable: fails inside the INSERT
    before = _counts()

    with pytest.raises(sqlite3.ProgrammingError):
        repo.create_instant_deck(user_id=None, display_name="Torn", cards=cards)

    assert _counts() == before
    assert _anon_users() == []


def test_the_same_failure_through_the_per_call_repos_leaves_a_partial_deck(initialized_db: str):
    """The tear `create_instant_deck` exists to prevent: DeckRepo.create
    and QuestionRepo.add each commit on their own connection."""
    deck_repo, question_repo = DeckRepo(), QuestionRepo()
    cards = _cards_failing_at_the_third()
    deck_id = deck_repo.create(initialized_db, "torn", display_name="Torn")

    with pytest.raises(RuntimeError):
        for card in cards:
            question_repo.add(
                initialized_db,
                deck_id,
                NewQuestion(
                    type=QuestionType.SHORT,
                    prompt=card["prompt"],
                    answer=card["answer"],
                    answer_regex=card["answer_regex"],
                ),
            )

    counts = _counts()
    assert counts["decks"] == 1
    assert counts["questions"] == 2
    assert counts["cards"] == 2
