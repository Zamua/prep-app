"""Tests for the /api/agent/run machine-to-machine SDK endpoint.

The endpoint speaks the same wire format the legacy agent-server
/run uses ({prompt, session_id?, resume_id?} → {stdout}) so the Go
worker can swap its BaseURL without code changes. We use FakeAgent
via set_agent() so the suite never burns real SDK credits.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from prep import agent as _agent_mod
from prep.agent.fake import FakeAgent
from prep.agent.port import AgentBudgetExhausted, AgentResult


@pytest.fixture
def fake_agent(monkeypatch: pytest.MonkeyPatch) -> FakeAgent:
    """Swap the process-singleton AgentPort with a FakeAgent for the
    duration of a test. Teardown restores the default selector path
    (set_agent(None)) so other tests' `is_available_for(uid)` checks
    don't see a stale captured adapter — which would otherwise leak
    True availability across tests, breaking siblings like the trivia
    redirect-on-no-agent test."""
    fake = FakeAgent()
    _agent_mod.set_agent(fake)
    yield fake
    _agent_mod.set_agent(None)


@pytest.fixture
def internal_token(monkeypatch: pytest.MonkeyPatch) -> str:
    token = "test-internal-secret"
    monkeypatch.setenv("PREP_INTERNAL_TOKEN", token)
    return token


def test_run_returns_stdout_wire_format(
    client: TestClient, fake_agent: FakeAgent, internal_token: str
):
    """The Go worker speaks {prompt} → {stdout}. New endpoint must
    match exactly so the BaseURL swap is a no-op on the worker side."""
    fake_agent.next_response = AgentResult(
        text="hello from the SDK", model="claude-sonnet-4-6", cost_usd=0.001
    )
    r = client.post(
        "/api/agent/run",
        json={"prompt": "say hi"},
        headers={"X-Internal-Token": internal_token},
    )
    assert r.status_code == 200
    assert r.json() == {"stdout": "hello from the SDK"}
    # The fake recorded the call so we know the adapter was invoked.
    assert len(fake_agent.calls) == 1
    assert fake_agent.calls[0]["prompt"] == "say hi"


def test_run_requires_internal_token(
    client: TestClient, fake_agent: FakeAgent, internal_token: str
):
    r = client.post("/api/agent/run", json={"prompt": "x"})
    assert r.status_code == 401


def test_run_503_when_internal_token_unconfigured(
    client: TestClient, fake_agent: FakeAgent, monkeypatch: pytest.MonkeyPatch
):
    """Fail-closed: no env var configured = service refuses everything.
    Prevents accidentally exposing the endpoint with no auth."""
    monkeypatch.delenv("PREP_INTERNAL_TOKEN", raising=False)
    r = client.post(
        "/api/agent/run",
        json={"prompt": "x"},
        headers={"X-Internal-Token": "anything"},
    )
    assert r.status_code == 503


def test_run_502_when_agent_unavailable(
    client: TestClient, fake_agent: FakeAgent, internal_token: str
):
    """Adapter raising AgentUnavailable maps to 502 + {error}."""
    fake_agent.raise_unavailable = True
    r = client.post(
        "/api/agent/run",
        json={"prompt": "x"},
        headers={"X-Internal-Token": internal_token},
    )
    assert r.status_code == 502
    assert "error" in r.json()


def test_run_429_with_kind_when_budget_exhausted(
    client: TestClient, fake_agent: FakeAgent, internal_token: str
):
    """AgentBudgetExhausted from the adapter (the SDK reported the
    user hit their monthly allocation) → 429 + a `kind` field UI
    can switch on to render the budget-specific message instead of
    the generic 'AI unavailable'."""

    async def _raise(*_args, **_kwargs):
        raise AgentBudgetExhausted("monthly allocation exhausted")

    fake_agent.run = _raise  # type: ignore[method-assign]
    r = client.post(
        "/api/agent/run",
        json={"prompt": "x"},
        headers={"X-Internal-Token": internal_token},
    )
    assert r.status_code == 429
    body = r.json()
    assert body.get("kind") == "budget_exhausted"
    assert "error" in body


def test_run_passes_through_model_and_reasoning_overrides(
    client: TestClient, fake_agent: FakeAgent, internal_token: str
):
    """When the worker overrides model/reasoning, the adapter sees
    those exact values (not the adapter defaults)."""
    client.post(
        "/api/agent/run",
        json={"prompt": "x", "model": "claude-haiku-4-5", "reasoning": "high"},
        headers={"X-Internal-Token": internal_token},
    )
    assert fake_agent.calls[-1]["model"] == "claude-haiku-4-5"
    assert fake_agent.calls[-1]["reasoning"] == "high"


def test_run_ignores_session_id_resume_id(
    client: TestClient, fake_agent: FakeAgent, internal_token: str
):
    """Legacy fields accepted-but-ignored. Endpoint should still 200."""
    r = client.post(
        "/api/agent/run",
        json={"prompt": "x", "session_id": "abc", "resume_id": "def"},
        headers={"X-Internal-Token": internal_token},
    )
    assert r.status_code == 200


# ---- /api/internal/record-review ---------------------------------------


def _seed_question(uid: str = "testuser@example.com"):
    """Create a deck + question + card row so the record-review endpoint
    has something to update. Returns the question id."""
    from prep.decks.entities import NewQuestion
    from prep.decks.repo import DeckRepo, QuestionRepo

    deck_id = DeckRepo().create(uid, "rev-test")
    qid = QuestionRepo().add(
        uid,
        deck_id,
        NewQuestion(
            type="short",
            topic="topic",
            prompt="What is 2+2?",
            answer="4",
            rubric="",
        ),
    )
    return qid


def test_record_review_writes_and_round_trips_idempotently(
    client: TestClient, initialized_db: str, internal_token: str
):
    """Happy path: post a grading, get back {step, next_due, interval_minutes}.
    Posting the same idempotency_key again returns the same payload
    without writing a second reviews row."""
    from prep.infrastructure.db import cursor

    uid = initialized_db
    qid = _seed_question(uid)

    r = client.post(
        "/api/internal/record-review",
        json={
            "user_id": uid,
            "question_id": qid,
            "result": "right",
            "user_answer": "4",
            "grader_notes": "",
            "idempotency_key": "wf-test-1",
        },
        headers={"X-Internal-Token": internal_token},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["step"] >= 1
    assert body["next_due"]
    assert body["interval_minutes"] >= 1

    # Idempotent retry: same key → same payload, no second review row.
    r2 = client.post(
        "/api/internal/record-review",
        json={
            "user_id": uid,
            "question_id": qid,
            "result": "right",
            "user_answer": "4",
            "grader_notes": "",
            "idempotency_key": "wf-test-1",
        },
        headers={"X-Internal-Token": internal_token},
    )
    assert r2.status_code == 200
    assert r2.json() == body
    with cursor() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM reviews WHERE question_id = ?", (qid,)).fetchone()
    assert n["n"] == 1


def test_record_review_rejects_cross_user_question(
    client: TestClient, initialized_db: str, internal_token: str
):
    """A worker that tries to record a review against another user's
    question gets 400 (non-retryable). Defense in depth — the workflow
    layer already shouldn't dispatch this, but the endpoint owns the
    ownership check now."""
    uid = initialized_db
    qid = _seed_question(uid)

    r = client.post(
        "/api/internal/record-review",
        json={
            "user_id": "wronguser@example.com",
            "question_id": qid,
            "result": "right",
            "user_answer": "",
            "grader_notes": "",
            "idempotency_key": "wf-cross-user",
        },
        headers={"X-Internal-Token": internal_token},
    )
    assert r.status_code == 400


def test_record_review_requires_internal_token(
    client: TestClient, initialized_db: str, internal_token: str
):
    r = client.post(
        "/api/internal/record-review",
        json={
            "user_id": initialized_db,
            "question_id": 1,
            "result": "right",
            "user_answer": "",
            "grader_notes": "",
            "idempotency_key": "k",
        },
    )
    assert r.status_code == 401
