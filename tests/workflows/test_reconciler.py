"""Tests for `prep.workflows.service.reconcile_active_workflows`.

The reconciler is the periodic background sweep that keeps
`active_workflows` accurate even when the user closed the
fragment-polling page before the workflow finished. It re-queries
Temporal for every non-terminal row, drives transitions through
`update_status` (so push notifications fire on the same code path the
fragment polls use), and prunes long-since-terminal rows.

Strategy: real sqlite (via the `initialized_db` fixture), a stub
Temporal client (programmable per-test), and a capturing push notifier.
The reconciler is pure-async; tests use pytest-asyncio's auto mode.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from prep.workflows import service as workflows_service
from prep.workflows.entities import WorkflowType
from prep.workflows.repo import ActiveWorkflowsRepo

# ---------- Fixtures / helpers ---------------------------------------------


class _StubNotifier:
    """Drop-in for `prep.notify.push.send_to_user`. Captures every call."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(
        self,
        user_login: str,
        title: str,
        body: str,
        url: str | None = None,
        source: str | None = None,
        tag: str | None = None,
    ) -> None:
        self.calls.append(
            {
                "user_login": user_login,
                "title": title,
                "body": body,
                "url": url,
                "source": source,
                "tag": tag,
            }
        )


class _FakeTemporal:
    """Programmable async stub mirroring `prep.temporal_client`'s shape.

    Each `set_*` configures the result for a given workflow_id; missing
    ids default to None (workflow's query handler is gone) for the
    progress calls and a NotFound-style error for describe.
    """

    class _NotFound(Exception):
        """Marker exception; messages match the substring the reconciler
        checks for ("not found")."""

        def __str__(self) -> str:  # pragma: no cover - trivial
            return "workflow not found in temporal namespace"

    def __init__(self) -> None:
        self._progress: dict[tuple[str, str], dict[str, Any] | None] = {}
        self._describe: dict[str, dict[str, Any] | Exception] = {}

    def set_progress(self, wid: str, kind: str, progress: dict[str, Any] | None) -> None:
        self._progress[(kind, wid)] = progress

    def set_describe(self, wid: str, desc: dict[str, Any] | Exception) -> None:
        self._describe[wid] = desc

    def set_gone(self, wid: str) -> None:
        """Both queries simulate the workflow having been deleted from
        temporal entirely — the reconciler should mark the row failed."""
        self._progress[("transform", wid)] = None
        self._progress[("plan", wid)] = None
        self._progress[("grade", wid)] = None
        self._progress[("trivia", wid)] = None
        self._describe[wid] = self._NotFound()

    async def get_transform_progress(self, wid: str):
        return self._progress.get(("transform", wid))

    async def get_plan_progress(self, wid: str):
        return self._progress.get(("plan", wid))

    async def get_grade_progress(self, wid: str):
        return self._progress.get(("grade", wid))

    async def get_trivia_progress(self, wid: str):
        return self._progress.get(("trivia", wid))

    async def describe_workflow(self, wid: str):
        v = self._describe.get(wid)
        if isinstance(v, Exception):
            raise v
        return v or {"status": "RUNNING"}


def _register(
    initialized_db: str,
    *,
    workflow_id: str = "transform-deck-1-abc123",
    workflow_type: WorkflowType = WorkflowType.TRANSFORM,
    deck_id: int | None = 1,
    deck_name: str | None = "go-systems",
    url_path: str | None = None,
    initial_status: str = "computing",
) -> str:
    workflows_service.register(
        user_login=initialized_db,
        workflow_id=workflow_id,
        workflow_type=workflow_type,
        deck_id=deck_id,
        deck_name=deck_name,
        url_path=url_path or f"/transform/{workflow_id}",
        initial_status=initial_status,
    )
    return workflow_id


# ---------- Tests -----------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_no_active_workflows(initialized_db: str):
    """Empty table → zeros across the board, no temporal queries fired."""
    notifier = _StubNotifier()
    summary = await workflows_service.reconcile_active_workflows(
        workflows_repo=ActiveWorkflowsRepo(),
        temporal_client=_FakeTemporal(),
        push_send_fn=notifier,
    )
    assert summary == workflows_service.ReconcileSummary(0, 0, 0, 0)
    assert notifier.calls == []


@pytest.mark.asyncio
async def test_reconcile_status_transition_to_awaiting_apply_fires_push(initialized_db: str):
    """The reconciler is the off-page replacement for the fragment poll:
    when temporal reports awaiting_apply, the row updates AND a push
    fires (idempotent via notified_action_at)."""
    wid = _register(initialized_db)
    fake = _FakeTemporal()
    fake.set_progress(wid, "transform", {"status": "awaiting_apply"})
    notifier = _StubNotifier()

    summary = await workflows_service.reconcile_active_workflows(
        workflows_repo=ActiveWorkflowsRepo(),
        temporal_client=fake,
        push_send_fn=notifier,
    )

    assert summary.checked == 1
    assert summary.status_changed == 1
    assert summary.notified == 1
    assert len(notifier.calls) == 1
    row = ActiveWorkflowsRepo().get(wid)
    assert row is not None
    assert row.status == "awaiting_apply"
    assert row.notified_action_at is not None


@pytest.mark.asyncio
async def test_reconcile_idempotent_on_repeated_ticks(initialized_db: str):
    """Two consecutive ticks landing on the same awaiting_apply status
    must produce exactly one push — the notified_action_at column
    short-circuits the second attempt."""
    wid = _register(initialized_db)
    fake = _FakeTemporal()
    fake.set_progress(wid, "transform", {"status": "awaiting_apply"})
    notifier = _StubNotifier()

    await workflows_service.reconcile_active_workflows(
        workflows_repo=ActiveWorkflowsRepo(),
        temporal_client=fake,
        push_send_fn=notifier,
    )
    second = await workflows_service.reconcile_active_workflows(
        workflows_repo=ActiveWorkflowsRepo(),
        temporal_client=fake,
        push_send_fn=notifier,
    )

    assert len(notifier.calls) == 1
    # Second pass observed the same status — no transition, no push.
    assert second.status_changed == 0
    assert second.notified == 0


@pytest.mark.asyncio
async def test_reconcile_terminal_transition_fires_push_and_sets_terminal_at(
    initialized_db: str,
):
    """A trivia-gen workflow that completes off-page: row gets stamped
    terminal_at AND a push fires (because no awaiting-action push
    preceded it — trivia-gen never asks the user for input)."""
    wid = _register(
        initialized_db,
        workflow_id="trivia-go-abc123",
        workflow_type=WorkflowType.TRIVIA_GEN,
        url_path="/deck/go-systems",
    )
    fake = _FakeTemporal()
    fake.set_progress(wid, "trivia", {"status": "done"})
    notifier = _StubNotifier()

    summary = await workflows_service.reconcile_active_workflows(
        workflows_repo=ActiveWorkflowsRepo(),
        temporal_client=fake,
        push_send_fn=notifier,
    )

    assert summary.status_changed == 1
    assert summary.notified == 1
    row = ActiveWorkflowsRepo().get(wid)
    assert row is not None
    assert row.terminal_at is not None
    assert row.notified_terminal_at is not None
    assert notifier.calls[0]["url"] == "/deck/go-systems"


@pytest.mark.asyncio
async def test_reconcile_terminal_via_describe_when_query_handler_gone(
    initialized_db: str,
):
    """A workflow that COMPLETED on temporal but whose query handler has
    aged out: progress returns None, describe returns COMPLETED. The
    reconciler must derive 'done' from describe and stamp the row."""
    wid = _register(initialized_db)
    fake = _FakeTemporal()
    fake.set_progress(wid, "transform", None)
    fake.set_describe(wid, {"status": "COMPLETED"})
    notifier = _StubNotifier()

    summary = await workflows_service.reconcile_active_workflows(
        workflows_repo=ActiveWorkflowsRepo(),
        temporal_client=fake,
        push_send_fn=notifier,
    )

    assert summary.status_changed == 1
    row = ActiveWorkflowsRepo().get(wid)
    assert row is not None
    assert row.status == "done"
    assert row.terminal_at is not None


@pytest.mark.asyncio
async def test_reconcile_prunes_old_terminal_rows(initialized_db: str):
    """Rows whose terminal_at is more than the prune window ago get
    hard-deleted. Recent terminal rows are kept."""
    repo = ActiveWorkflowsRepo()
    # In-flight row — must survive.
    _register(initialized_db, workflow_id="alive-wid")
    # Recent terminal — must survive (within prune window).
    _register(initialized_db, workflow_id="recent-wid")
    repo.update_status("recent-wid", "done")
    repo.set_terminal_at("recent-wid")
    # Ancient terminal — must be pruned.
    _register(initialized_db, workflow_id="ancient-wid")
    repo.update_status("ancient-wid", "done")
    twenty_five_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    repo.set_terminal_at("ancient-wid", terminal_at=twenty_five_hours_ago)

    fake = _FakeTemporal()
    # The alive row will be queried — let it stay in flight.
    fake.set_progress("alive-wid", "transform", {"status": "computing"})

    summary = await workflows_service.reconcile_active_workflows(
        workflows_repo=repo,
        temporal_client=fake,
        push_send_fn=_StubNotifier(),
    )

    assert summary.pruned == 1
    assert repo.get("ancient-wid") is None
    assert repo.get("recent-wid") is not None
    assert repo.get("alive-wid") is not None


@pytest.mark.asyncio
async def test_reconcile_handles_gone_workflow(initialized_db: str):
    """When both progress AND describe fail with a not-found-style
    error, the reconciler marks the row failed + stamps terminal_at so
    the next pass can prune it."""
    wid = _register(initialized_db)
    fake = _FakeTemporal()
    fake.set_gone(wid)
    notifier = _StubNotifier()

    await workflows_service.reconcile_active_workflows(
        workflows_repo=ActiveWorkflowsRepo(),
        temporal_client=fake,
        push_send_fn=notifier,
    )

    row = ActiveWorkflowsRepo().get(wid)
    assert row is not None
    assert row.status == "failed"
    assert row.terminal_at is not None


@pytest.mark.asyncio
async def test_reconcile_one_bad_row_does_not_abort_tick(initialized_db: str):
    """A failure querying one workflow must NOT prevent the rest of the
    table from being reconciled."""
    bad = _register(initialized_db, workflow_id="bad-wid")
    good = _register(initialized_db, workflow_id="good-wid")

    class _PartiallyBrokenTemporal(_FakeTemporal):
        async def get_transform_progress(self, wid):
            if wid == bad:
                raise RuntimeError("temporal blew up on this id")
            return await super().get_transform_progress(wid)

    fake = _PartiallyBrokenTemporal()
    fake.set_progress(good, "transform", {"status": "awaiting_apply"})
    fake.set_describe(bad, RuntimeError("temporal blew up on describe too"))
    notifier = _StubNotifier()

    summary = await workflows_service.reconcile_active_workflows(
        workflows_repo=ActiveWorkflowsRepo(),
        temporal_client=fake,
        push_send_fn=notifier,
    )

    # 2 rows checked; the bad one couldn't be classified (transient
    # error, neither 'not found' nor a valid status), so it's a no-op
    # for this tick. The good one transitioned.
    assert summary.checked == 2
    assert summary.status_changed == 1
    good_row = ActiveWorkflowsRepo().get(good)
    assert good_row is not None
    assert good_row.status == "awaiting_apply"
    # Bad row is still in flight — we'll retry next tick.
    bad_row = ActiveWorkflowsRepo().get(bad)
    assert bad_row is not None
    assert bad_row.terminal_at is None
