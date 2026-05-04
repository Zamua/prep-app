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

    from prep.auth.repo import UserRepo

    UserRepo().upsert("bob@example.com")
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
    from prep.auth.repo import UserRepo

    UserRepo().upsert("bob@example.com")
    with pytest.raises(ValueError, match="not owned"):
        review_repo.record("bob@example.com", qid, "right", user_answer="A")


def test_count_due_for_user_starts_at_one(review_repo: ReviewRepo, seeded_deck):
    """A fresh card lands due immediately (next_due = ts at insert)."""
    user, _, _ = seeded_deck
    assert review_repo.count_due_for_user(user) >= 1


def test_count_due_for_user_excludes_trivia_decks(review_repo: ReviewRepo, initialized_db: str):
    """Trivia-deck cards have a `cards` row by accident of the schema
    (every question gets one), but they're not studied via the SRS
    flow. count_due_for_user must skip them so the SRS digest /
    when-ready path doesn't trigger 'N cards due to study' for
    trivia notifications."""
    from prep.decks.entities import NewQuestion, QuestionType
    from prep.decks.repo import DeckRepo, QuestionRepo

    user = initialized_db
    trivia_id = DeckRepo().create_trivia(user, "trivia-deck", topic="x", interval_minutes=30)
    QuestionRepo().add(
        user,
        trivia_id,
        NewQuestion(type=QuestionType.SHORT, prompt="?", answer="A"),
    )
    # Trivia deck has notifications_enabled=1 by default but is type='trivia';
    # the gated count must still be zero.
    assert review_repo.count_due_for_user(user) == 0


def test_due_breakdown_excludes_trivia_decks(initialized_db: str):
    """Same gate as count_due_for_user — trivia decks must not appear
    in the SRS digest body breakdown."""
    from prep.decks.entities import NewQuestion, QuestionType
    from prep.decks.repo import DeckRepo, QuestionRepo

    user = initialized_db
    srs_id = DeckRepo().create(user, "srs-deck")
    QuestionRepo().add(user, srs_id, NewQuestion(type=QuestionType.MCQ, prompt="?", answer="A"))
    trivia_id = DeckRepo().create_trivia(user, "trivia-deck", topic="x", interval_minutes=30)
    QuestionRepo().add(
        user, trivia_id, NewQuestion(type=QuestionType.SHORT, prompt="?", answer="A")
    )
    breakdown = DeckRepo().due_breakdown(user)
    names = [b[0] for b in breakdown]
    assert "srs-deck" in names
    assert "trivia-deck" not in names


def test_find_active_decodes_cached_verdict_and_state(session_repo: SessionRepo, seeded_deck):
    """Regression for the v0.14.0 bug Sean hit on /study/<deck>/begin:
    the underlying sqlite query for find_active_session_for_deck does
    SELECT *, returning last_answered_verdict + last_answered_state
    as JSON-string TEXT columns. _row_to_session must decode them
    before constructing the StudySession entity (which types both as
    dict | None and would otherwise raise pydantic ValidationError).

    Reproduces the bug by writing a session with a non-empty cached
    verdict + state via record_answer_sync, then resuming via
    find_active_for_deck and asserting the entity comes back with
    decoded dicts."""
    user, deck_id, qid = seeded_deck
    sid = session_repo.create(user, deck_id, "Mac")

    # Mark the question as answered so the session has cached verdict
    # + state populated. record_answer_sync writes them as JSON text
    # in sqlite — exactly the rowshape find_active returns.
    verdict = {
        "result": "wrong",
        "feedback": "[parsed array of ['informal', 'formal or plural']]",
    }
    state = {"step": 0, "next_due": "2026-04-30T01:00:00Z", "interval_minutes": 10}
    session_repo.record_answer_sync(
        user, sid, qid, expected_version=1, user_answer="x", verdict=verdict, state=state
    )

    resumed = session_repo.find_active_for_deck(user, deck_id)
    assert resumed is not None
    assert isinstance(resumed.last_answered_verdict, dict)
    assert resumed.last_answered_verdict["result"] == "wrong"
    assert isinstance(resumed.last_answered_state, dict)
    assert resumed.last_answered_state["step"] == 0
    assert resumed.last_answered_state["interval_minutes"] == 10
