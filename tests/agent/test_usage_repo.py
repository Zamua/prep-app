"""Tests for AgentUsageRepo + token-hash rollup behavior.

Token-scoped means: multiple users sharing one OAuth token sum into
one bucket. The hash is what the rollup keys on; the user_login is
a secondary dimension preserved for breakdowns but never the
primary key.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient  # noqa: F401 — only for fixture wiring

from prep.agent.usage import AgentUsageRepo, hash_token


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def test_hash_token_is_stable_and_hex():
    h = hash_token("sk-ant-oat01-fake-token")
    assert h == hash_token("sk-ant-oat01-fake-token")
    assert len(h) == 64  # sha256 hex
    assert all(c in "0123456789abcdef" for c in h)


def test_record_and_monthly_cost_for_single_token(initialized_db: str):
    repo = AgentUsageRepo()
    th = hash_token("token-a")
    repo.record(
        token_hash=th,
        model="claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.0123,
        user_login=initialized_db,
    )
    repo.record(
        token_hash=th,
        model="claude-sonnet-4-6",
        input_tokens=200,
        output_tokens=80,
        cost_usd=0.0250,
        user_login=initialized_db,
    )
    month_start = _iso(datetime.now(timezone.utc) - timedelta(days=30))
    total = repo.monthly_cost(th, month_start_iso=month_start)
    assert abs(total - 0.0373) < 1e-9
    assert repo.call_count(th, month_start_iso=month_start) == 2


def test_monthly_cost_partitioned_by_token(initialized_db: str):
    """Two tokens must NOT share a rollup. The whole point of the
    token-scoped key is that the credit pools are independent."""
    repo = AgentUsageRepo()
    th_a = hash_token("token-a")
    th_b = hash_token("token-b")
    repo.record(
        token_hash=th_a,
        model="m",
        input_tokens=10,
        output_tokens=10,
        cost_usd=1.0,
        user_login=initialized_db,
    )
    repo.record(
        token_hash=th_b,
        model="m",
        input_tokens=10,
        output_tokens=10,
        cost_usd=99.0,
        user_login=initialized_db,
    )
    month_start = _iso(datetime.now(timezone.utc) - timedelta(days=30))
    assert repo.monthly_cost(th_a, month_start_iso=month_start) == 1.0
    assert repo.monthly_cost(th_b, month_start_iso=month_start) == 99.0


def test_monthly_cost_returns_zero_when_no_calls(initialized_db: str):
    """Empty bucket must return 0.0, not None / KeyError. Settings UI
    renders the number unconditionally so this is load-bearing."""
    repo = AgentUsageRepo()
    th = hash_token("never-used")
    month_start = _iso(datetime.now(timezone.utc) - timedelta(days=30))
    assert repo.monthly_cost(th, month_start_iso=month_start) == 0.0
    assert repo.call_count(th, month_start_iso=month_start) == 0


def test_monthly_window_excludes_older_rows(initialized_db: str):
    """Calls before the window aren't counted. We don't backdate
    records in production (they get datetime.now() inside record()),
    but the query has to respect the window or rollovers break."""
    repo = AgentUsageRepo()
    th = hash_token("token-window")
    repo.record(
        token_hash=th,
        model="m",
        input_tokens=10,
        output_tokens=10,
        cost_usd=5.0,
        user_login=initialized_db,
    )
    # Window starts in the future → no rows match
    future = _iso(datetime.now(timezone.utc) + timedelta(days=1))
    assert repo.monthly_cost(th, month_start_iso=future) == 0.0
