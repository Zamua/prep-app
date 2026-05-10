"""Service-layer tests for the study bounded context.

Mirrors the shape of `tests/decks/test_service.py`: the synchronous
use cases run against the per-test sqlite via `initialized_db`; the
async grading orchestration runs against a `FakeTemporalClient` that
records what it was called with.
"""

from __future__ import annotations

from typing import Any

import pytest

from prep.decks.entities import NewQuestion, QuestionType
from prep.decks.repo import DeckRepo, QuestionRepo
from prep.study import service as svc
from prep.study.entities import SessionState, SessionStatus
from prep.study.repo import ReviewRepo, SessionRepo, StaleVersionError


@pytest.fixture
def repos(initialized_db: str):
    return SessionRepo(), ReviewRepo()


@pytest.fixture
def seeded_deck(initialized_db: str) -> tuple[str, int, int]:
    """(user_id, deck_id, qid) — fresh deck with a single mcq card."""
    user = initialized_db
    deck_id = DeckRepo().create(user, "study-svc-test")
    qid = QuestionRepo().add(
        user,
        deck_id,
        NewQuestion(type=QuestionType.MCQ, prompt="2+2?", answer="4", choices=["3", "4", "5"]),
    )
    return user, deck_id, qid


# ---- Sync use cases -------------------------------------------------------


def test_start_session_returns_id_and_seeds_active_session(repos, seeded_deck):
    sess_repo, _ = repos
    user, deck_id, _qid = seeded_deck
    sid = svc.start_session(sess_repo, user, deck_id, device_label="Mac")
    assert sid
    s = svc.get_session(sess_repo, user, sid)
    assert s is not None
    assert s.status is SessionStatus.ACTIVE
    assert s.state is SessionState.AWAITING_ANSWER


def test_find_active_session_round_trips(repos, seeded_deck):
    sess_repo, _ = repos
    user, deck_id, _qid = seeded_deck
    sid = svc.start_session(sess_repo, user, deck_id, "iPhone")
    found = svc.find_active_session(sess_repo, user, deck_id)
    assert found is not None
    assert found.id == sid


def test_find_active_session_none_when_no_session(repos, seeded_deck):
    sess_repo, _ = repos
    user, deck_id, _qid = seeded_deck
    assert svc.find_active_session(sess_repo, user, deck_id) is None


def test_submit_sync_answer_records_review_and_bumps_version(repos, seeded_deck):
    """Happy path for the sync grader path: review row written, session
    row carries the cached verdict + state, version increments."""
    sess_repo, rev_repo = repos
    user, deck_id, qid = seeded_deck
    sid = svc.start_session(sess_repo, user, deck_id, "Mac")
    s = svc.get_session(sess_repo, user, sid)
    assert s is not None
    state, new_v = svc.submit_sync_answer(
        sess_repo,
        rev_repo,
        user_id=user,
        sid=sid,
        qid=qid,
        expected_version=s.version,
        user_answer="4",
        verdict={"result": "right"},
    )
    assert state.step >= 1
    assert new_v == s.version + 1
    after = svc.get_session(sess_repo, user, sid)
    assert after is not None
    assert after.state is SessionState.SHOWING_RESULT
    assert after.last_answered_qid == qid


def test_submit_sync_answer_stale_version_raises(repos, seeded_deck):
    """Passing an outdated expected_version → StaleVersionError. The
    route layer maps this to 409."""
    sess_repo, rev_repo = repos
    user, deck_id, qid = seeded_deck
    sid = svc.start_session(sess_repo, user, deck_id, "Mac")
    with pytest.raises(StaleVersionError):
        svc.submit_sync_answer(
            sess_repo,
            rev_repo,
            user_id=user,
            sid=sid,
            qid=qid,
            expected_version=999,  # never the real version
            user_answer="4",
            verdict={"result": "right"},
        )


def test_advance_session_completes_when_no_due_card_left(repos, seeded_deck):
    """After answering the only card in the deck, advancing the session
    transitions to status='completed' (no more due cards)."""
    sess_repo, rev_repo = repos
    user, deck_id, qid = seeded_deck
    sid = svc.start_session(sess_repo, user, deck_id, "Mac")
    s = svc.get_session(sess_repo, user, sid)
    assert s is not None
    _state, v1 = svc.submit_sync_answer(
        sess_repo,
        rev_repo,
        user_id=user,
        sid=sid,
        qid=qid,
        expected_version=s.version,
        user_answer="4",
        verdict={"result": "right"},
    )
    svc.advance_session(sess_repo, user, sid, v1)
    after = svc.get_session(sess_repo, user, sid)
    assert after is not None
    assert after.status is SessionStatus.COMPLETED


def test_abandon_session_marks_status(repos, seeded_deck):
    sess_repo, _ = repos
    user, deck_id, _qid = seeded_deck
    sid = svc.start_session(sess_repo, user, deck_id, "Mac")
    svc.abandon_session(sess_repo, user, sid)
    after = svc.get_session(sess_repo, user, sid)
    assert after is not None
    assert after.status is SessionStatus.ABANDONED


# ---- Async grading orchestration ------------------------------------------


class FakeTemporalClient:
    """Records every call so tests can inspect what the service
    threaded through. Mimics `prep.temporal_client`'s public surface."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def start_grading(self, qid, deck_name, user_answer, idk, *, user_id):
        self.calls.append(
            (
                "start_grading",
                {
                    "qid": qid,
                    "deck_name": deck_name,
                    "user_answer": user_answer,
                    "idk": idk,
                    "user_id": user_id,
                },
            )
        )

        class _R:
            workflow_id = "wf-stub-1"

        return _R()

    async def get_grade_progress(self, wid):
        self.calls.append(("get_grade_progress", {"wid": wid}))
        return {"status": "running"}

    async def get_grade_result(self, wid):
        self.calls.append(("get_grade_result", {"wid": wid}))
        return {"verdict": {"result": "right"}}


@pytest.fixture
def code_seeded_deck(initialized_db: str) -> tuple[str, int, int]:
    """A deck with one code-typed question (free-text grading path)."""
    user = initialized_db
    deck_id = DeckRepo().create(user, "code-deck")
    qid = QuestionRepo().add(
        user,
        deck_id,
        NewQuestion(
            type=QuestionType.CODE,
            prompt="def add(a,b):",
            answer="return a+b",
            language="python",
        ),
    )
    return user, deck_id, qid


async def test_start_grading_marks_session_grading_and_returns_wid(repos, code_seeded_deck):
    sess_repo, _ = repos
    user, deck_id, qid = code_seeded_deck
    sid = svc.start_session(sess_repo, user, deck_id, "Mac")
    s = svc.get_session(sess_repo, user, sid)
    assert s is not None

    client: Any = FakeTemporalClient()
    wid = await svc.start_grading(
        client,
        sess_repo,
        user_id=user,
        sid=sid,
        qid=qid,
        deck_name="code-deck",
        expected_version=s.version,
        user_answer="return a+b",
        idk=False,
    )
    assert wid == "wf-stub-1"
    after = svc.get_session(sess_repo, user, sid)
    assert after is not None
    assert after.state is SessionState.GRADING
    assert after.current_grading_workflow_id == "wf-stub-1"
    # The temporal client received the parameters verbatim.
    assert client.calls[0] == (
        "start_grading",
        {
            "qid": qid,
            "deck_name": "code-deck",
            "user_answer": "return a+b",
            "idk": False,
            "user_id": user,
        },
    )


async def test_grading_progress_and_result_pass_through():
    client: Any = FakeTemporalClient()
    p = await svc.get_grading_progress(client, "wf-1")
    r = await svc.get_grading_result(client, "wf-1")
    assert p == {"status": "running"}
    assert r == {"verdict": {"result": "right"}}


def test_grading_landed_writes_cached_verdict_to_session(repos, code_seeded_deck):
    """Reconciliation path: route polled, saw a terminal verdict, calls
    grading_landed → the session row carries the cached verdict and
    transitions to showing-result."""
    sess_repo, _ = repos
    user, deck_id, qid = code_seeded_deck
    sid = svc.start_session(sess_repo, user, deck_id, "Mac")
    s = svc.get_session(sess_repo, user, sid)
    assert s is not None
    sess_repo.set_grading(user, sid, qid, "wf-1", s.version)
    svc.grading_landed(
        sess_repo,
        user_id=user,
        sid=sid,
        question_id=qid,
        workflow_id="wf-1",
        verdict={"result": "right"},
        state={"step": 1, "next_due": "2026-05-10T00:00:00Z", "interval_minutes": 10},
    )
    after = svc.get_session(sess_repo, user, sid)
    assert after is not None
    assert after.state is SessionState.SHOWING_RESULT
    assert after.last_answered_verdict == {"result": "right"}
