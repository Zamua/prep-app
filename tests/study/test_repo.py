"""Integration tests for prep.study.repo against real sqlite."""

from __future__ import annotations

import pytest

from prep.decks.entities import NewQuestion, QuestionType
from prep.decks.repo import DeckRepo, QuestionRepo
from prep.study.entities import (
    CardState,
    RecentSession,
    SessionState,
    SessionStatus,
    StudySession,
)
from prep.study.repo import ReviewRepo, SessionRepo


@pytest.fixture
def session_repo(initialized_db: str) -> SessionRepo:
    return SessionRepo()


@pytest.fixture
def review_repo(initialized_db: str) -> ReviewRepo:
    return ReviewRepo()


@pytest.fixture
def seeded_deck(initialized_db: str) -> tuple[str, int, int]:
    """Returns (user_id, deck_id, qid) — a freshly-created deck with one card."""
    user = initialized_db
    deck_id = DeckRepo().create(user, "study-test")
    qid = QuestionRepo().add(
        user,
        deck_id,
        NewQuestion(type=QuestionType.MCQ, prompt="?", answer="A", choices=["A", "B"]),
    )
    return user, deck_id, qid


# ---- SessionRepo ------------------------------------------------------


def test_create_returns_session_id(session_repo: SessionRepo, seeded_deck):
    user, deck_id, _ = seeded_deck
    sid = session_repo.create(user, deck_id, "Mac")
    assert sid
    assert len(sid) >= 8


def test_get_returns_entity(session_repo: SessionRepo, seeded_deck):
    user, deck_id, _ = seeded_deck
    sid = session_repo.create(user, deck_id, "iPhone")
    s = session_repo.get(user, sid)
    assert isinstance(s, StudySession)
    assert s is not None
    assert s.id == sid
    assert s.status is SessionStatus.ACTIVE
    assert s.state is SessionState.AWAITING_ANSWER
    assert s.device_label == "iPhone"


def test_get_user_isolation(session_repo: SessionRepo, seeded_deck):
    """A get() with the wrong user_id returns None."""
    user, deck_id, _ = seeded_deck
    sid = session_repo.create(user, deck_id, "Mac")

    from prep import db as _db

    _db.upsert_user("bob@example.com")
    assert session_repo.get("bob@example.com", sid) is None
    assert session_repo.get(user, sid) is not None


def test_find_active_for_deck(session_repo: SessionRepo, seeded_deck):
    user, deck_id, _ = seeded_deck
    assert session_repo.find_active_for_deck(user, deck_id) is None
    sid = session_repo.create(user, deck_id, "Mac")
    found = session_repo.find_active_for_deck(user, deck_id)
    assert found is not None
    assert found.id == sid


def test_list_recent_sessions(session_repo: SessionRepo, seeded_deck):
    user, deck_id, _ = seeded_deck
    session_repo.create(user, deck_id, "Mac")
    recents = session_repo.list_recent(user)
    assert len(recents) == 1
    assert isinstance(recents[0], RecentSession)
    assert recents[0].deck_name == "study-test"


def test_update_draft_bumps_version(session_repo: SessionRepo, seeded_deck):
    user, deck_id, _ = seeded_deck
    sid = session_repo.create(user, deck_id, "Mac")
    assert session_repo.get(user, sid).version == 1
    new_v = session_repo.update_draft(user, sid, "draft text", expected_version=1)
    assert new_v == 2
    assert session_repo.get(user, sid).version == 2


def test_update_draft_stale_version_raises(session_repo: SessionRepo, seeded_deck):
    """Stale-version error is the canary the route layer turns into a
    409 banner. We assert by classname rather than `pytest.raises(X)`
    because conftest's per-test reload of prep.db rebinds the
    StaleVersionError class — the module-level alias here would point
    at a pre-reload identity and miss. The exception's class name +
    `current_version` attribute are the stable contract."""
    user, deck_id, _ = seeded_deck
    sid = session_repo.create(user, deck_id, "Mac")
    session_repo.update_draft(user, sid, "first", expected_version=1)  # bumps to 2

    raised: Exception | None = None
    try:
        session_repo.update_draft(user, sid, "stale", expected_version=1)
    except Exception as e:
        raised = e
    assert raised is not None
    assert raised.__class__.__name__ == "StaleVersionError"
    assert getattr(raised, "current_version", None) == 2


def test_abandon_terminates_session(session_repo: SessionRepo, seeded_deck):
    user, deck_id, _ = seeded_deck
    sid = session_repo.create(user, deck_id, "Mac")
    session_repo.abandon(user, sid)
    s = session_repo.get(user, sid)
    assert s is not None
    assert s.status is SessionStatus.ABANDONED


def test_device_label_from_ua(session_repo: SessionRepo, initialized_db: str):
    """Light UA sniffing — purely for the recent-sessions list label."""
    assert session_repo.device_label_from_ua(None) == "unknown device"
    assert session_repo.device_label_from_ua("Mozilla/5.0 (iPhone)") == "iPhone"
    assert session_repo.device_label_from_ua("Mozilla/5.0 (Macintosh)") == "Mac"


# ---- ReviewRepo -------------------------------------------------------


def test_record_review_advances_step(review_repo: ReviewRepo, seeded_deck):
    user, _, qid = seeded_deck
    state = review_repo.record(user, qid, "right", user_answer="A")
    assert isinstance(state, CardState)
    assert state.step == 1  # was 0, advances to 1 on right
    assert state.next_due
    assert state.interval_minutes == 24 * 60  # step 1 = 1 day


def test_record_review_wrong_resets_to_zero(review_repo: ReviewRepo, seeded_deck):
    user, _, qid = seeded_deck
    review_repo.record(user, qid, "right", user_answer="A")  # → step 1
    state = review_repo.record(user, qid, "wrong", user_answer="B")
    assert state.step == 0
    assert state.interval_minutes == 10  # step 0 = 10 min


def test_record_review_other_users_question_raises(review_repo: ReviewRepo, seeded_deck):
    """Defense in depth: record_review checks ownership even if the
    route forgets to."""
    user, _, qid = seeded_deck
    from prep import db as _db

    _db.upsert_user("bob@example.com")
    with pytest.raises(ValueError, match="not owned"):
        review_repo.record("bob@example.com", qid, "right", user_answer="A")


def test_count_due_for_user_starts_at_one(review_repo: ReviewRepo, seeded_deck):
    """A fresh card lands due immediately (next_due = ts at insert)."""
    user, _, _ = seeded_deck
    assert review_repo.count_due_for_user(user) >= 1
