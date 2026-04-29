"""Tests for prep.decks.service.

Synchronous use cases run against the real (temp-path) sqlite via
the same fixtures the repo tests use. Async use cases run against
a `FakeTemporalClient` that records what was called, so we can
verify the service threads parameters through correctly without
spinning up a real Temporal server.
"""

from __future__ import annotations

from typing import Any

import pytest

from prep.decks import service as svc
from prep.decks.entities import (
    DeckSummary,
    NewQuestion,
    Question,
    QuestionType,
)
from prep.decks.repo import DeckRepo, QuestionRepo

# ============================================================================
# Synchronous use cases
# ============================================================================


@pytest.fixture
def repos(initialized_db: str):
    return DeckRepo(), QuestionRepo()


def test_create_and_list_decks(repos: tuple[DeckRepo, QuestionRepo], initialized_db: str):
    deck_repo, _q = repos
    user = initialized_db
    svc.create_deck(deck_repo, user, "go-systems")
    summaries = svc.list_user_decks(deck_repo, user)
    assert len(summaries) == 1
    assert isinstance(summaries[0], DeckSummary)
    assert summaries[0].name == "go-systems"


def test_add_then_get_question(repos: tuple[DeckRepo, QuestionRepo], initialized_db: str):
    deck_repo, q_repo = repos
    user = initialized_db
    deck_id = svc.create_deck(deck_repo, user, "go-systems")
    qid = svc.add_question(
        q_repo,
        user,
        deck_id,
        NewQuestion(type=QuestionType.MCQ, prompt="?", answer="A"),
    )
    q = svc.get_question(q_repo, user, qid)
    assert isinstance(q, Question)
    assert q.id == qid


def test_suspend_then_unsuspend_toggles(repos: tuple[DeckRepo, QuestionRepo], initialized_db: str):
    deck_repo, q_repo = repos
    user = initialized_db
    deck_id = svc.create_deck(deck_repo, user, "go-systems")
    qid = svc.add_question(
        q_repo, user, deck_id, NewQuestion(type=QuestionType.MCQ, prompt="?", answer="A")
    )
    assert svc.get_question(q_repo, user, qid).suspended is False
    svc.suspend_question(q_repo, user, qid)
    assert svc.get_question(q_repo, user, qid).suspended is True
    svc.unsuspend_question(q_repo, user, qid)
    assert svc.get_question(q_repo, user, qid).suspended is False


def test_delete_deck_cascades(repos: tuple[DeckRepo, QuestionRepo], initialized_db: str):
    deck_repo, q_repo = repos
    user = initialized_db
    deck_id = svc.create_deck(deck_repo, user, "doomed")
    qid = svc.add_question(
        q_repo, user, deck_id, NewQuestion(type=QuestionType.MCQ, prompt="?", answer="x")
    )
    svc.delete_deck(deck_repo, user, "doomed")
    assert svc.get_question(q_repo, user, qid) is None


# ============================================================================
# Async orchestration — fake-client tests
# ============================================================================


class FakeTemporalClient:
    """Records every call so tests can assert the service threaded
    parameters through correctly. Each method returns whatever was
    set on the corresponding `_returns` attribute, so tests can
    seed return values for the workflow-handle / progress-dict path."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        # Default returns; tests override as needed.
        self.start_plan_returns: Any = type("WfHandle", (), {"workflow_id": "wf-123"})()
        self.start_transform_returns: Any = type("WfHandle", (), {"workflow_id": "wf-456"})()
        self.plan_progress_returns: dict = {"status": "planning", "items": []}
        self.transform_progress_returns: dict = {"status": "computing"}
        self.transform_result_returns: dict = {"items": []}

    async def start_plan_generate(self, **kwargs):
        self.calls.append(("start_plan_generate", kwargs))
        return self.start_plan_returns

    async def signal_plan_feedback(self, wid: str, feedback: str):
        self.calls.append(("signal_plan_feedback", {"wid": wid, "feedback": feedback}))

    async def signal_plan_accept(self, wid: str):
        self.calls.append(("signal_plan_accept", {"wid": wid}))

    async def signal_plan_reject(self, wid: str):
        self.calls.append(("signal_plan_reject", {"wid": wid}))

    async def get_plan_progress(self, wid: str):
        self.calls.append(("get_plan_progress", {"wid": wid}))
        return self.plan_progress_returns

    async def start_transform(self, **kwargs):
        self.calls.append(("start_transform", kwargs))
        return self.start_transform_returns

    async def signal_apply_transform(self, wid: str):
        self.calls.append(("signal_apply_transform", {"wid": wid}))

    async def signal_reject_transform(self, wid: str):
        self.calls.append(("signal_reject_transform", {"wid": wid}))

    async def get_transform_progress(self, wid: str):
        self.calls.append(("get_transform_progress", {"wid": wid}))
        return self.transform_progress_returns

    async def get_transform_result(self, wid: str):
        self.calls.append(("get_transform_result", {"wid": wid}))
        return self.transform_result_returns


# ---- plan-first generation ---------------------------------------------


async def test_start_plan_generation_threads_params():
    client = FakeTemporalClient()
    handle = await svc.start_plan_generation(
        client,
        user_id="alice@example.com",
        deck_id=7,
        deck_name="go-systems",
        prompt="Generate 10 cards on Go channels.",
    )
    assert handle.workflow_id == "wf-123"
    assert client.calls == [
        (
            "start_plan_generate",
            {
                "user_id": "alice@example.com",
                "deck_id": 7,
                "deck_name": "go-systems",
                "prompt": "Generate 10 cards on Go channels.",
            },
        )
    ]


async def test_plan_signals_pass_through():
    client = FakeTemporalClient()
    await svc.submit_plan_feedback(client, "wf-1", "more on mutexes")
    await svc.accept_plan(client, "wf-1")
    await svc.reject_plan(client, "wf-2")
    names = [name for name, _ in client.calls]
    assert names == ["signal_plan_feedback", "signal_plan_accept", "signal_plan_reject"]


async def test_get_plan_progress_returns_client_payload():
    client = FakeTemporalClient()
    client.plan_progress_returns = {"status": "ready", "items": [{"title": "T1"}]}
    out = await svc.get_plan_progress(client, "wf-x")
    assert out == {"status": "ready", "items": [{"title": "T1"}]}


# ---- deck-wide transform -----------------------------------------------


async def test_start_deck_transform_threads_existing_prompts():
    """The service requires the caller to gather existing prompts
    (no repo lookup hidden inside the use case). Verify they're
    passed through to the workflow."""
    client = FakeTemporalClient()
    await svc.start_deck_transform(
        client,
        user_id="alice@example.com",
        deck_id=7,
        deck_name="go-systems",
        instruction="Make every prompt shorter.",
        existing_prompts=["P1", "P2", "P3"],
    )
    assert client.calls[0][0] == "start_transform"
    args = client.calls[0][1]
    assert args["existing_prompts"] == ["P1", "P2", "P3"]
    assert args["instruction"] == "Make every prompt shorter."


async def test_transform_signals_pass_through():
    client = FakeTemporalClient()
    await svc.apply_transform(client, "wf-1")
    await svc.reject_transform(client, "wf-2")
    names = [name for name, _ in client.calls]
    assert names == ["signal_apply_transform", "signal_reject_transform"]
