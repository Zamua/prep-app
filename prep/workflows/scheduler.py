"""Workflow reconciler — periodic loop that keeps active_workflows in
sync with Temporal even when the user isn't actively polling.

Counterpart to `prep.notify.scheduler`. Where notify decides when to
fire a digest / when-ready push for SRS cards, this module re-queries
Temporal for every non-terminal active_workflows row and drives the
state machine forward (status transitions, push notifications, prune
of long-since-terminal rows).

Why both: the fragment-poll path (htmx hx-trigger="every 2s" on the
transform/plan/grading pages) keeps a row accurate ONLY while the
user has the page open. Close the tab mid-flight and the row gets
stuck. The reconciler is the fallback that runs every 30s on the
server so the badge + push notifications stay accurate regardless of
which pages the user has open.

Tick model: every PREP_WORKFLOW_RECONCILE_SECONDS (default 30s), call
`service.reconcile_active_workflows`, log a one-line stats summary.
Each tick is wrapped in try/except so one bad query doesn't break the
loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from prep import temporal_client
from prep.notify.push import send_to_user
from prep.workflows.repo import ActiveWorkflowsRepo
from prep.workflows.service import reconcile_active_workflows

_log = logging.getLogger("prep.workflows")


def _interval_seconds() -> float:
    """Env-overridable tick interval. Default 30s — fast enough that a
    finished workflow's "you're done" push lands within ~30s of the
    actual completion, slow enough that the temporal query load is
    negligible (most ticks find an empty in-flight set)."""
    raw = os.environ.get("PREP_WORKFLOW_RECONCILE_SECONDS", "30")
    try:
        v = float(raw)
        return v if v > 0 else 30.0
    except ValueError:
        return 30.0


async def _tick() -> None:
    """One reconciler iteration. Logs a one-line stats summary at INFO."""
    started = time.monotonic()
    summary = await reconcile_active_workflows(
        workflows_repo=ActiveWorkflowsRepo(),
        temporal_client=temporal_client,
        push_send_fn=send_to_user,
    )
    duration_ms = int((time.monotonic() - started) * 1000)
    _log.info(
        "workflow reconciler tick: checked=%d status_changed=%d notified=%d "
        "pruned=%d (duration=%d ms)",
        summary.checked,
        summary.status_changed,
        summary.notified,
        summary.pruned,
        duration_ms,
    )


async def _scheduler_loop(interval_seconds: float) -> None:
    """Forever loop: tick, log, sleep. Each tick is wrapped so a single
    failure (transient temporal error, repo glitch) doesn't break the
    loop. The next tick will pick up where we left off."""
    while True:
        try:
            await _tick()
        except Exception as e:
            _log.exception("workflow reconciler tick threw: %s", e)
        await asyncio.sleep(interval_seconds)


def start_workflows_scheduler(interval_seconds: float | None = None) -> None:
    """Launch the background reconciler task on the running event loop.
    Call once from app startup. Idempotent — a second call is a no-op.

    `interval_seconds` overrides the env-derived default (used in
    tests; production passes None so the env var wins)."""
    if getattr(start_workflows_scheduler, "_started", False):
        return
    interval = interval_seconds if interval_seconds is not None else _interval_seconds()
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
    loop.create_task(_scheduler_loop(interval))
    start_workflows_scheduler._started = True
    _log.info("workflow reconciler scheduler started (tick=%.1fs)", interval)
