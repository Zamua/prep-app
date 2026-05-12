"""Tests for `prep.workflows.scheduler` — the periodic reconciler loop.

The reconciler loop is fire-and-forget: a single tick failure must not
break the loop. The test below feeds a controlled exception on the
first tick, asserts the second tick still runs.

The integration with `service.reconcile_active_workflows` is covered
by `test_reconciler.py`; here we exercise just the scheduler shell.

Logger note: `prep.app` configures `logging.getLogger("prep")` with
`propagate = False` once another test has imported the app module.
caplog hooks into the root logger, so log records from `prep.workflows`
won't reach it after that import-side-effect has happened. The fixture
below restores propagation for the test's duration so caplog can see
the records, and reverts after.
"""

from __future__ import annotations

import asyncio
import logging

import pytest


@pytest.fixture
def workflows_logger_propagating():
    """Temporarily re-enable propagation on the `prep` logger tree so
    caplog (which attaches to root) can observe records emitted by
    `prep.workflows`. See module docstring for the why."""
    log = logging.getLogger("prep")
    prev = log.propagate
    log.propagate = True
    try:
        yield
    finally:
        log.propagate = prev


@pytest.mark.asyncio
async def test_scheduler_loop_continues_after_a_failing_tick(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    workflows_logger_propagating,
):
    """A tick that raises must NOT kill the loop — the next iteration
    should still fire. Pattern: one-shot poisoned tick, then a clean
    tick that flips a flag we can assert on."""
    from prep.workflows import scheduler as sched

    tick_count = 0
    second_tick_ran = asyncio.Event()

    async def _ticks_with_one_bad_apple():
        nonlocal tick_count
        tick_count += 1
        if tick_count == 1:
            raise RuntimeError("synthetic temporal blow-up on tick 1")
        # Second tick — succeed and signal the test.
        second_tick_ran.set()

    monkeypatch.setattr(sched, "_tick", _ticks_with_one_bad_apple)

    # Use a tiny interval so we don't wait 30s for the second tick.
    task = asyncio.create_task(sched._scheduler_loop(interval_seconds=0.01))
    try:
        with caplog.at_level(logging.ERROR, logger="prep.workflows"):
            await asyncio.wait_for(second_tick_ran.wait(), timeout=2.0)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # The first tick's exception was logged but didn't kill the loop.
    assert tick_count >= 2
    assert any(
        "workflow reconciler tick threw" in r.message for r in caplog.records
    ), "expected the tick failure to be logged at ERROR"


@pytest.mark.asyncio
async def test_tick_logs_summary_stats(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    workflows_logger_propagating,
):
    """`_tick` should log a one-line summary at INFO so operators can
    grep docker logs for reconciler health."""
    from prep.workflows import scheduler as sched
    from prep.workflows.service import ReconcileSummary

    async def _fake_reconcile(**kw):
        return ReconcileSummary(checked=3, status_changed=1, notified=1, pruned=2)

    monkeypatch.setattr(sched, "reconcile_active_workflows", _fake_reconcile)

    with caplog.at_level(logging.INFO, logger="prep.workflows"):
        await sched._tick()

    msgs = [r.message for r in caplog.records if "workflow reconciler tick" in r.message]
    assert msgs, "expected an INFO log line summarising the tick"
    line = msgs[0]
    assert "checked=3" in line
    assert "status_changed=1" in line
    assert "notified=1" in line
    assert "pruned=2" in line


def test_interval_seconds_env_override(monkeypatch: pytest.MonkeyPatch):
    """Operators can shorten the tick via PREP_WORKFLOW_RECONCILE_SECONDS
    for debugging without rebuilding the image."""
    from prep.workflows import scheduler as sched

    monkeypatch.setenv("PREP_WORKFLOW_RECONCILE_SECONDS", "5")
    assert sched._interval_seconds() == 5.0

    # Garbage env var falls back to the default rather than crashing
    # the loop start.
    monkeypatch.setenv("PREP_WORKFLOW_RECONCILE_SECONDS", "not-a-number")
    assert sched._interval_seconds() == 30.0

    # Zero / negative also fall back to the default — a 0s tick would
    # spin the loop and hose CPU.
    monkeypatch.setenv("PREP_WORKFLOW_RECONCILE_SECONDS", "0")
    assert sched._interval_seconds() == 30.0
