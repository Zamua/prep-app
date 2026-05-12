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


async def test_start_deck_transform_passes_scope_and_target(
    repos: tuple[DeckRepo, QuestionRepo], initialized_db: str
):
    """Deck-scope transform → scope='deck', target_id=deck_id."""
    deck_repo, _q_repo = repos
    user = initialized_db
    deck_id = deck_repo.create(user, "go-systems")
    client = FakeTemporalClient()
    await svc.start_deck_transform(
        client,
        deck_repo=deck_repo,
        user_id=user,
        deck_id=deck_id,
        prompt="Make every prompt shorter.",
    )
    assert client.calls[0][0] == "start_transform"
    args = client.calls[0][1]
    assert args["scope"] == "deck"
    assert args["target_id"] == deck_id
    assert args["prompt"] == "Make every prompt shorter."


async def test_start_deck_transform_threads_deck_context_prompt(
    repos: tuple[DeckRepo, QuestionRepo], initialized_db: str
):
    """When the deck has a context_prompt, the service looks it up
    and passes it through to the temporal client as
    `deck_context_prompt` — the worker injects it into claude's
    prompt so a deck-wide rewrite reads the deck's overall theme."""
    deck_repo, _q_repo = repos
    user = initialized_db
    deck_id = deck_repo.create(
        user, "world-history", context_prompt="World history from antiquity to 1900."
    )
    client = FakeTemporalClient()
    await svc.start_deck_transform(
        client,
        deck_repo=deck_repo,
        user_id=user,
        deck_id=deck_id,
        prompt="Add 3 cards on the silk road.",
    )
    args = client.calls[0][1]
    assert args["deck_context_prompt"] == "World history from antiquity to 1900."


async def test_start_deck_transform_empty_context_for_legacy_deck(
    repos: tuple[DeckRepo, QuestionRepo], initialized_db: str
):
    """Legacy decks with no context_prompt set should produce
    deck_context_prompt='' — the worker drops the preamble block
    when it's empty rather than rendering an empty heading."""
    deck_repo, _q_repo = repos
    user = initialized_db
    deck_id = deck_repo.create(user, "legacy-deck", context_prompt=None)
    client = FakeTemporalClient()
    await svc.start_deck_transform(
        client,
        deck_repo=deck_repo,
        user_id=user,
        deck_id=deck_id,
        prompt="Polish wording.",
    )
    args = client.calls[0][1]
    assert args["deck_context_prompt"] == ""


async def test_start_card_transform_passes_scope_and_target(
    repos: tuple[DeckRepo, QuestionRepo], initialized_db: str
):
    """Card-scope transform → scope='card', target_id=qid. Workflow
    auto-applies on completion (no apply/reject signals expected)."""
    deck_repo, q_repo = repos
    user = initialized_db
    deck_id = deck_repo.create(user, "scratch")
    qid = q_repo.add(user, deck_id, NewQuestion(type=QuestionType.MCQ, prompt="q", answer="a"))
    client = FakeTemporalClient()
    await svc.start_card_transform(
        client,
        deck_repo=deck_repo,
        question_repo=q_repo,
        user_id=user,
        qid=qid,
        prompt="Tighten the wording.",
    )
    assert client.calls[0][0] == "start_transform"
    args = client.calls[0][1]
    assert args["scope"] == "card"
    assert args["target_id"] == qid


async def test_start_card_transform_threads_deck_context_prompt(
    repos: tuple[DeckRepo, QuestionRepo], initialized_db: str
):
    """Card-scope: service walks question → deck → context_prompt
    and passes it through. A single-card rewrite still benefits
    from knowing the deck's overall theme."""
    deck_repo, q_repo = repos
    user = initialized_db
    deck_id = deck_repo.create(
        user, "databases", context_prompt="ACID, isolation levels, replication."
    )
    qid = q_repo.add(user, deck_id, NewQuestion(type=QuestionType.SHORT, prompt="?", answer="!"))
    client = FakeTemporalClient()
    await svc.start_card_transform(
        client,
        deck_repo=deck_repo,
        question_repo=q_repo,
        user_id=user,
        qid=qid,
        prompt="Make this clearer.",
    )
    args = client.calls[0][1]
    assert args["deck_context_prompt"] == "ACID, isolation levels, replication."


async def test_start_card_transform_unknown_qid_passes_empty_context(
    repos: tuple[DeckRepo, QuestionRepo], initialized_db: str
):
    """Defense-in-depth: if the question lookup fails (shouldn't
    happen — the route gate runs first — but service should not
    crash), the call still goes through with empty context."""
    deck_repo, q_repo = repos
    client = FakeTemporalClient()
    await svc.start_card_transform(
        client,
        deck_repo=deck_repo,
        question_repo=q_repo,
        user_id=initialized_db,
        qid=999999,
        prompt="Fix it.",
    )
    args = client.calls[0][1]
    assert args["deck_context_prompt"] == ""


async def test_transform_signals_pass_through():
    client = FakeTemporalClient()
    await svc.apply_transform(client, "wf-1")
    await svc.reject_transform(client, "wf-2")
    names = [name for name, _ in client.calls]
    assert names == ["signal_apply_transform", "signal_reject_transform"]


# ============================================================================
# build_transform_view_ctx
# ============================================================================


def _seed_simple_deck_with_card(
    deck_repo: DeckRepo, q_repo: QuestionRepo, user: str
) -> tuple[int, int]:
    """Helper: create one deck + one question. Returns (deck_id, qid)."""
    deck_id = deck_repo.create(user, "scratch", context_prompt=None)
    qid = q_repo.add(
        user,
        deck_id,
        NewQuestion(
            type=QuestionType.SHORT,
            prompt="Capital of France?",
            answer="Paris",
            topic="geo",
            rubric="must mention Paris",
        ),
    )
    return deck_id, qid


def test_build_transform_view_ctx_deck_scope_resolves_deck_name(
    repos: tuple[DeckRepo, QuestionRepo], initialized_db: str
):
    """Deck-scope: deck_name comes from the target deck id directly."""
    deck_repo, q_repo = repos
    user = initialized_db
    deck_id, _qid = _seed_simple_deck_with_card(deck_repo, q_repo, user)
    ctx = svc.build_transform_view_ctx(
        deck_repo=deck_repo,
        question_repo=q_repo,
        user_id=user,
        scope="deck",
        target_id=deck_id,
        progress=None,
    )
    assert ctx.deck_name == "scratch"
    assert ctx.modification_diffs == []
    assert ctx.deletion_decks == {}
    assert ctx.move_source_decks == {}


def test_build_transform_view_ctx_card_scope_walks_question_to_deck(
    repos: tuple[DeckRepo, QuestionRepo], initialized_db: str
):
    """Card-scope: deck_name resolves via the question's deck_id."""
    deck_repo, q_repo = repos
    user = initialized_db
    _deck_id, qid = _seed_simple_deck_with_card(deck_repo, q_repo, user)
    ctx = svc.build_transform_view_ctx(
        deck_repo=deck_repo,
        question_repo=q_repo,
        user_id=user,
        scope="card",
        target_id=qid,
        progress=None,
    )
    assert ctx.deck_name == "scratch"


def test_build_transform_view_ctx_modifications_diff_old_vs_new(
    repos: tuple[DeckRepo, QuestionRepo], initialized_db: str
):
    """Modification diff carries the old DB shape and claude's
    proposed new shape side-by-side. Fields not present on the
    modification fall through to the old value."""
    deck_repo, q_repo = repos
    user = initialized_db
    deck_id, qid = _seed_simple_deck_with_card(deck_repo, q_repo, user)
    progress = {
        "plan": {
            "modifications": [
                {
                    "question_id": qid,
                    "prompt": "What's the capital of France?",
                    # rubric explicitly carried; topic untouched (falls
                    # through to the old value).
                    "rubric": "spelled correctly",
                }
            ]
        }
    }
    ctx = svc.build_transform_view_ctx(
        deck_repo=deck_repo,
        question_repo=q_repo,
        user_id=user,
        scope="deck",
        target_id=deck_id,
        progress=progress,
    )
    assert len(ctx.modification_diffs) == 1
    d = ctx.modification_diffs[0]
    assert d.question_id == qid
    assert d.deck_name == "scratch"
    assert d.old["prompt"] == "Capital of France?"
    assert d.new["prompt"] == "What's the capital of France?"
    assert d.old["rubric"] == "must mention Paris"
    assert d.new["rubric"] == "spelled correctly"
    # Untouched field: new falls through to old (here both = "geo").
    assert d.new["topic"] == "geo"


def test_build_transform_view_ctx_skips_unknown_modification_qid(
    repos: tuple[DeckRepo, QuestionRepo], initialized_db: str
):
    """Modifications referencing a question that doesn't exist (or
    belongs to another user) are silently dropped — the diff list
    only contains rows we can actually preview."""
    deck_repo, q_repo = repos
    user = initialized_db
    deck_id, _qid = _seed_simple_deck_with_card(deck_repo, q_repo, user)
    progress = {
        "plan": {
            "modifications": [
                {"question_id": 99999, "prompt": "ghost"},  # not owned by user
                {"question_id": None, "prompt": "no id"},
            ]
        }
    }
    ctx = svc.build_transform_view_ctx(
        deck_repo=deck_repo,
        question_repo=q_repo,
        user_id=user,
        scope="deck",
        target_id=deck_id,
        progress=progress,
    )
    assert ctx.modification_diffs == []


def test_build_transform_view_ctx_resolves_reorganize_groupings(
    repos: tuple[DeckRepo, QuestionRepo], initialized_db: str
):
    """Reorganize plan: deletion_decks + move_source_decks should
    pre-resolve each question's source deck name so the template can
    group by deck without per-row jinja lookups."""
    deck_repo, q_repo = repos
    user = initialized_db
    deck_id, qid_a = _seed_simple_deck_with_card(deck_repo, q_repo, user)
    qid_b = q_repo.add(
        user,
        deck_id,
        NewQuestion(type=QuestionType.SHORT, prompt="Largest planet?", answer="Jupiter"),
    )
    progress = {
        "plan": {
            "deletions": [qid_a],
            "card_moves": [{"question_id": qid_b, "dest_deck_name": "elsewhere"}],
        }
    }
    ctx = svc.build_transform_view_ctx(
        deck_repo=deck_repo,
        question_repo=q_repo,
        user_id=user,
        scope="deck",
        target_id=deck_id,
        progress=progress,
    )
    assert ctx.deletion_decks == {qid_a: "scratch"}
    assert ctx.move_source_decks == {qid_b: "scratch"}
    assert ctx.deck_id_to_name == {deck_id: "scratch"}
