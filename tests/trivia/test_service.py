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


# ---- classify_grading -------------------------------------------------


@pytest.mark.parametrize(
    "expected",
    [
        "Paris",
        "id Software",
        "Bobby Prince",
        "Leonardo da Vinci",
        "1944",
        "3.14",
        "50%",
        "USA",
        "",  # empty falls through to deterministic safely
    ],
)
def test_classify_grading_picks_deterministic_for_short_answers(expected):
    assert svc.classify_grading(expected) == "deterministic"


@pytest.mark.parametrize(
    "expected",
    [
        "Both Lennon and McCartney",  # 4 tokens
        "About thirty-one and a half million",  # sentence-shaped
        "Anywhere from 50 to 100 milliseconds",  # 6 tokens
        "Fast, cheap, and good — pick two.",  # punctuation
        "It depends on the consistency model.",  # sentence
    ],
)
def test_classify_grading_picks_claude_for_long_or_complex(expected):
    assert svc.classify_grading(expected) == "claude"


# ---- claude_grade -----------------------------------------------------


def test_claude_grade_parses_right_verdict(monkeypatch):
    monkeypatch.setattr(
        svc,
        "run_prompt",
        lambda _p, **_k: '{"verdict": "right", "feedback": "Same fact, different phrasing."}',
    )
    out = svc.claude_grade(prompt="Q?", expected="A long answer", given="A different phrasing")
    assert out == {
        "correct": True,
        "feedback": "Same fact, different phrasing.",
        "regex_update": None,
    }


def test_claude_grade_parses_wrong_verdict(monkeypatch):
    monkeypatch.setattr(
        svc,
        "run_prompt",
        lambda _p, **_k: '```json\n{"verdict": "wrong", "feedback": "Missed the key fact."}\n```',
    )
    out = svc.claude_grade(prompt="Q?", expected="something specific", given="something else")
    assert out == {
        "correct": False,
        "feedback": "Missed the key fact.",
        "regex_update": None,
    }


def test_claude_grade_returns_validated_regex_update_on_alt_form(monkeypatch):
    """Initial-grade path now also returns regex_update when claude
    proposes one for a legitimate alternative form (synonym/abbr).
    The grader validates it (compiles + matches both literal and
    user form) before passing to the caller."""
    monkeypatch.setattr(
        svc,
        "run_prompt",
        lambda _p, **_k: (
            '{"verdict": "right", "feedback": "WAL is a standard abbreviation.",'
            ' "regex_update": "(write[- ]?ahead log|wal)"}'
        ),
    )
    out = svc.claude_grade(
        prompt="What ensures durability?",
        expected="write-ahead log",
        given="wal",
        current_regex="write[- ]?ahead log",
    )
    assert out["correct"] is True
    assert out["regex_update"] == "(write[- ]?ahead log|wal)"


def test_claude_grade_drops_regex_update_when_invalid(monkeypatch):
    """A claude-proposed regex that doesn't match the canonical
    expected answer is dropped — the route must never persist a
    regex that breaks future grading of the literal answer."""
    monkeypatch.setattr(
        svc,
        "run_prompt",
        lambda _p, **_k: (
            '{"verdict": "right", "feedback": "ok",'
            ' "regex_update": "(wal|wahl)"}'  # doesn't match "write-ahead log"
        ),
    )
    out = svc.claude_grade(
        prompt="?",
        expected="write-ahead log",
        given="wal",
        current_regex=None,
    )
    assert out["correct"] is True
    assert out["regex_update"] is None


def test_claude_grade_blank_answer_short_circuits(monkeypatch):
    monkeypatch.setattr(svc, "run_prompt", lambda *_a, **_k: pytest.fail("should not call agent"))
    out = svc.claude_grade(prompt="Q?", expected="A long answer here", given="   ")
    assert out["correct"] is False


def test_claude_grade_falls_back_on_agent_unavailable(monkeypatch):
    def boom(*_a, **_k):
        raise AgentUnavailable("agent down")

    monkeypatch.setattr(svc, "run_prompt", boom)
    # Agent down → fall back to deterministic; "London" != "Paris" → wrong.
    out = svc.claude_grade(prompt="Capital of France?", expected="Paris", given="London")
    assert out["correct"] is False
    assert "claude was unreachable" in out["feedback"]


def test_claude_grade_falls_back_on_bad_json(monkeypatch):
    monkeypatch.setattr(svc, "run_prompt", lambda *_a, **_k: "not even json")
    out = svc.claude_grade(prompt="Q?", expected="anything", given="something")
    # No JSON → fall back to deterministic match (empty token-set tests, returns False here).
    assert isinstance(out["correct"], bool)
    assert "malformed JSON" in out["feedback"]


# ---- looks_like_paraphrase --------------------------------------------


@pytest.mark.parametrize(
    "expected,given,is_paraphrase",
    [
        # User wrote a long explanation instead of the short answer.
        (
            "Key redistribution",
            "it prevents a cascade of work being reshuffled between servers",
            True,
        ),
        # Same length → not a paraphrase signal.
        ("Paris", "London", False),
        # Empty → not a paraphrase, just a non-attempt.
        ("Paris", "", False),
        ("Paris", "   ", False),
        # 1 extra token isn't enough to escalate.
        ("Newton", "Isaac Newton", False),
        # 2 extra tokens IS enough (the threshold).
        ("Newton", "Isaac the great Newton", True),
    ],
)
def test_looks_like_paraphrase(expected, given, is_paraphrase):
    assert svc.looks_like_paraphrase(expected=expected, given=given) is is_paraphrase
