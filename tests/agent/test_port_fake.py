"""Tests for the agent port + the in-memory FakeAgent test double.

Verifies the contract the rest of the codebase depends on:
- Calling `run` records the call (so SUTs that depend on the agent
  can be asserted against)
- Canned responses come out in order
- `raise_unavailable=True` flips the exception path
- The `AgentResult` dataclass round-trips its fields
"""

from __future__ import annotations

import asyncio

import pytest

from prep.agent.fake import FakeAgent
from prep.agent.port import AgentResult, AgentUnavailable


def test_fake_records_each_call():
    fake = FakeAgent()
    asyncio.run(fake.run("first", model="claude-haiku-4-5"))
    asyncio.run(fake.run("second", reasoning="high"))
    assert len(fake.calls) == 2
    assert fake.calls[0]["prompt"] == "first"
    assert fake.calls[0]["model"] == "claude-haiku-4-5"
    assert fake.calls[1]["reasoning"] == "high"


def test_fake_returns_default_when_unconfigured():
    fake = FakeAgent()
    result = asyncio.run(fake.run("anything"))
    assert isinstance(result, AgentResult)
    assert result.cost_usd == 0.0
    # The default model echoes the caller's pick when given.
    result2 = asyncio.run(fake.run("anything", model="claude-opus-4-8"))
    assert result2.model == "claude-opus-4-8"


def test_fake_drains_responses_queue_in_order():
    a = AgentResult(text="A", model="m", cost_usd=0.01)
    b = AgentResult(text="B", model="m", cost_usd=0.02)
    fake = FakeAgent(responses=[a, b])
    r1 = asyncio.run(fake.run("p1"))
    r2 = asyncio.run(fake.run("p2"))
    assert r1.text == "A"
    assert r2.text == "B"


def test_fake_unavailable_flag_raises():
    fake = FakeAgent(raise_unavailable=True)
    with pytest.raises(AgentUnavailable):
        asyncio.run(fake.run("anything"))


def test_agent_result_is_frozen():
    """Sanity: result is immutable so callers can pass it around
    without worrying about defensive copies."""
    from dataclasses import FrozenInstanceError

    r = AgentResult(text="x", model="m")
    with pytest.raises(FrozenInstanceError):
        r.text = "y"  # type: ignore[misc]
