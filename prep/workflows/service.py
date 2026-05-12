"""Application service for workflow-tracking.

Three entry points used by the rest of the app:

- `register(...)` — called by start_* routes/services immediately
  after Temporal hands back a workflow id. Inserts a row in
  `active_workflows` so the badge picks it up on the next poll.

- `update_status(...)` — called by every fragment-status route after
  it computes the current status. Diffs against the prior status,
  fires push notifications on the awaiting-action and terminal
  transitions (idempotent via the notified_*_at columns), and stamps
  terminal_at when the workflow first reaches a terminal status.

- `reconcile_active_workflows(...)` — called periodically from
  `prep.workflows.scheduler`. Walks every non-terminal row in the
  table, re-queries Temporal for the truth-of-state, drives each row
  forward via `update_status` (so push notifications fire idempotently
  through the same code path the fragment polls use), and prunes
  long-since-terminal rows. This keeps the table accurate even when
  the user closes the fragment-polling page before the workflow
  finishes.

All three functions swallow errors — the workflow tracker is
observability, not the path that matters. A failure here must NOT
break the start route, the fragment poll, or the scheduler loop.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from prep.workflows.entities import (
    ActiveWorkflow,
    WorkflowType,
    is_action_required,
    is_terminal,
)
from prep.workflows.repo import (
    RECONCILER_PRUNE_WINDOW_SECONDS,
    ActiveWorkflowsRepo,
)

_log = logging.getLogger("prep.workflows")


def register(
    *,
    user_login: str,
    workflow_id: str,
    workflow_type: WorkflowType,
    deck_id: int | None,
    deck_name: str | None,
    url_path: str,
    initial_status: str = "computing",
    repo: ActiveWorkflowsRepo | None = None,
) -> None:
    """Record a just-started workflow.

    Never raises — a failure here would cascade into the start route
    failing for the user, which is worse than missing a badge entry.
    """
    try:
        (repo or ActiveWorkflowsRepo()).register(
            workflow_id=workflow_id,
            user_login=user_login,
            workflow_type=workflow_type,
            deck_id=deck_id,
            deck_name=deck_name,
            url_path=url_path,
            initial_status=initial_status,
        )
    except Exception as e:
        _log.warning("workflow register failed for %s: %s", workflow_id, e)


def update_status(
    *,
    workflow_id: str,
    new_status: str,
    repo: ActiveWorkflowsRepo | None = None,
    notifier=None,
) -> ActiveWorkflow | None:
    """Update a tracked workflow's status, fire push notifications on
    awaiting-action / terminal transitions (idempotent), and stamp
    terminal_at when first terminal.

    `notifier` is the push.send_to_user function — passed in to keep
    this module's tests free of the VAPID / pywebpush dependency
    chain. Production callers pass `prep.notify.push.send_to_user`.

    Returns the updated entity for the route to consume, or None if
    no matching row exists (the workflow was never registered — e.g.
    started before this feature shipped). Never raises.
    """
    try:
        r = repo or ActiveWorkflowsRepo()
        prev = r.get(workflow_id)
        if prev is None:
            return None

        # Cheap no-op when status hasn't moved.
        if prev.status == new_status:
            return prev

        r.update_status(workflow_id, new_status)

        # Fire push notification on the awaiting-action transition.
        if (
            is_action_required(new_status)
            and not is_action_required(prev.status)
            and not prev.notified_action_at
        ):
            _fire_push(
                notifier,
                user_login=prev.user_login,
                kind="action",
                workflow=prev,
                new_status=new_status,
            )
            r.mark_notified(workflow_id, "action")

        # Fire push notification on the terminal transition. The
        # awaiting-action notification already covered the "needs you"
        # case; only fire here if we DIDN'T already fire the
        # action-required push (otherwise reject-after-review would
        # double-ping the user).
        if is_terminal(new_status) and not prev.notified_terminal_at:
            r.set_terminal_at(workflow_id)
            # Skip the terminal push if we already pinged for awaiting-action
            # AND the terminal state is one the user explicitly chose
            # (apply/reject from the review screen). Apply→done /
            # reject→rejected are the user's own decisions; a notification
            # for them is just noise. We still fire for unattended
            # terminal states (the deck-creation grading/trivia-gen
            # cases that never hit awaiting-action).
            if not prev.notified_action_at:
                _fire_push(
                    notifier,
                    user_login=prev.user_login,
                    kind="terminal",
                    workflow=prev,
                    new_status=new_status,
                )
                r.mark_notified(workflow_id, "terminal")

        return r.get(workflow_id)
    except Exception as e:
        _log.warning("workflow update_status failed for %s: %s", workflow_id, e)
        return None


def _fire_push(
    notifier,
    *,
    user_login: str,
    kind: str,
    workflow: ActiveWorkflow,
    new_status: str,
) -> None:
    """Build + send the push. Swallows errors so a push failure can't
    break the status-update path."""
    if notifier is None:
        # Late-bind the real notifier to keep workflow → notify a
        # one-way import (tests pass an explicit notifier).
        try:
            from prep.notify.push import send_to_user as notifier  # type: ignore[no-redef]
        except Exception as e:
            _log.warning("push notifier import failed: %s", e)
            return
    label = workflow.deck_name or workflow.workflow_type.value.replace("_", " ")
    if kind == "action":
        title = "Prep — action required"
        body = _action_body(workflow.workflow_type, label)
    else:
        title = "Prep — done"
        body = _terminal_body(workflow.workflow_type, label, new_status)
    try:
        notifier(
            user_login,
            title,
            body,
            url=workflow.url_path,
            source="workflow",
            tag=f"workflow-{workflow.workflow_id}",
        )
    except Exception as e:
        _log.warning("push send failed for %s: %s", workflow.workflow_id, e)


def _action_body(wf_type: WorkflowType, label: str) -> str:
    if wf_type is WorkflowType.TRANSFORM:
        return f"Transform on {label} is ready to review."
    if wf_type is WorkflowType.PLAN:
        return f"Plan for {label} is ready to review."
    return f"{label} needs your attention."


def _terminal_body(wf_type: WorkflowType, label: str, status: str) -> str:
    if status in ("failed", "FAILED"):
        return f"{wf_type.value.replace('_', ' ')} on {label} failed."
    if wf_type is WorkflowType.TRIVIA_GEN:
        return f"Trivia for {label} is ready."
    if wf_type is WorkflowType.GRADING:
        return f"Grading is done — {label}."
    if wf_type is WorkflowType.PLAN:
        return f"Plan for {label} is done."
    return f"Transform on {label} is done."


# ----- Reconciler ----------------------------------------------------------


@dataclass(frozen=True)
class ReconcileSummary:
    """Per-tick stats from `reconcile_active_workflows`.

    The scheduler logs these at INFO so a quick `docker logs | grep
    'workflow reconciler'` shows whether the loop is healthy and
    making progress."""

    checked: int = 0
    status_changed: int = 0
    notified: int = 0
    pruned: int = 0


class _TemporalClientProtocol(Protocol):
    """Minimal surface the reconciler needs from `prep.temporal_client`.

    Inlined as a Protocol so tests can pass any stub with the same
    coroutine shape without depending on temporalio. Production passes
    the `prep.temporal_client` module itself."""

    async def describe_workflow(self, workflow_id: str) -> dict[str, Any]: ...
    async def get_transform_progress(self, workflow_id: str) -> dict[str, Any] | None: ...
    async def get_plan_progress(self, workflow_id: str) -> dict[str, Any] | None: ...
    async def get_grade_progress(self, workflow_id: str) -> dict[str, Any] | None: ...
    async def get_trivia_progress(self, workflow_id: str) -> dict[str, Any] | None: ...


async def _query_progress(
    temporal_client: Any, wf_type: WorkflowType, workflow_id: str
) -> dict[str, Any] | None:
    """Dispatch to the type-specific progress query. Returns the same
    `{status: ...}` dict every workflow exposes via its getXProgress
    handler, or None if the query handler is gone (closed workflow)."""
    if wf_type is WorkflowType.TRANSFORM:
        return await temporal_client.get_transform_progress(workflow_id)
    if wf_type is WorkflowType.PLAN:
        return await temporal_client.get_plan_progress(workflow_id)
    if wf_type is WorkflowType.GRADING:
        return await temporal_client.get_grade_progress(workflow_id)
    if wf_type is WorkflowType.TRIVIA_GEN:
        return await temporal_client.get_trivia_progress(workflow_id)
    return None


def _derive_status(progress: dict[str, Any] | None, desc: dict[str, Any] | None) -> str:
    """Same precedence the fragment routes use: live progress status
    wins; fall back to the describe()-derived status when the query
    handler is gone. Map COMPLETED → 'done' so the row lands in the
    terminal bucket without depending on entities.TERMINAL_STATUSES
    catching the verbatim describe-status (it does, but it reads
    cleaner in the UI and in push body text)."""
    raw = (progress or {}).get("status") or (desc or {}).get("status") or ""
    if raw == "COMPLETED":
        return "done"
    if raw == "FAILED":
        return "failed"
    if raw in ("CANCELED", "TERMINATED"):
        return "rejected"
    return raw


async def reconcile_active_workflows(
    *,
    workflows_repo: ActiveWorkflowsRepo,
    temporal_client: _TemporalClientProtocol,
    push_send_fn: Callable[..., Awaitable[None] | None] | None = None,
    now: datetime | None = None,
    prune_window_seconds: int = RECONCILER_PRUNE_WINDOW_SECONDS,
) -> ReconcileSummary:
    """Walk every non-terminal row, re-query Temporal, drive updates
    through `update_status` (which handles push fan-out idempotently),
    and prune ancient terminal rows.

    Designed to be safe to run repeatedly even if individual rows are
    in odd shapes: every per-row query is wrapped, and a failure on
    one row doesn't abort the rest of the tick.

    Returns a `ReconcileSummary` for the scheduler to log."""
    _ = now or datetime.now(timezone.utc)  # reserved for future "stuck workflow" detection
    rows = workflows_repo.list_non_terminal()

    checked = 0
    status_changed = 0
    notified = 0

    for wf in rows:
        checked += 1
        try:
            prev_status = wf.status
            prev_notified_action = wf.notified_action_at
            prev_notified_terminal = wf.notified_terminal_at

            new_status = await _query_one(temporal_client, wf)

            if not new_status:
                # Couldn't determine a status (e.g. transient temporal
                # blip). Leave the row as-is; we'll retry next tick.
                continue

            if new_status == prev_status:
                continue

            updated = update_status(
                workflow_id=wf.workflow_id,
                new_status=new_status,
                repo=workflows_repo,
                notifier=push_send_fn,
            )
            if updated is None:
                continue

            status_changed += 1
            # Count a notification iff one of the notified_*_at columns
            # transitioned from NULL to a value in this update.
            if (updated.notified_action_at and not prev_notified_action) or (
                updated.notified_terminal_at and not prev_notified_terminal
            ):
                notified += 1
        except Exception as e:
            _log.exception("reconciler row failed for %s: %s", wf.workflow_id, e)

    pruned = 0
    try:
        pruned = workflows_repo.prune_terminal_older_than(window_seconds=prune_window_seconds)
    except Exception as e:
        _log.exception("reconciler prune failed: %s", e)

    return ReconcileSummary(
        checked=checked,
        status_changed=status_changed,
        notified=notified,
        pruned=pruned,
    )


async def _query_one(temporal_client: _TemporalClientProtocol, wf: ActiveWorkflow) -> str | None:
    """Pull progress+describe for one workflow and derive a status.

    Returns None when both queries fail in ways we can't classify
    (transient — leave the row alone). Returns 'failed' when the
    workflow is GONE from temporal (e.g. namespace cleanup deleted it
    entirely) so the row gets stamped terminal and pruned on a
    subsequent tick.

    A workflow that's still in flight but whose query handler hasn't
    set status yet returns ''; the caller treats '' == prev_status as
    a no-op."""
    progress: dict[str, Any] | None = None
    desc: dict[str, Any] | None = None
    progress_err: Exception | None = None
    desc_err: Exception | None = None

    try:
        progress = await _query_progress(temporal_client, wf.workflow_type, wf.workflow_id)
    except Exception as e:
        progress_err = e

    try:
        desc = await temporal_client.describe_workflow(wf.workflow_id)
    except Exception as e:
        desc_err = e

    # describe() failing AND progress query failing — usually means the
    # workflow record itself is gone from temporal (not just the in-memory
    # query handler). Mark failed so the row is stamped terminal and ages
    # out. The classifier is intentionally loose: any error message
    # containing 'not found', 'NOT_FOUND', or 'no rows' counts; we don't
    # depend on a specific exception class so this is resilient to
    # temporalio SDK upgrades.
    if desc_err is not None and progress is None:
        msg = (str(desc_err) + " " + str(progress_err or "")).lower()
        if "not found" in msg or "no rows" in msg or "no workflow" in msg:
            return "failed"
        # Some other transient temporal error — leave the row for the
        # next tick.
        _log.warning(
            "reconciler couldn't reach temporal for %s: desc=%r progress=%r",
            wf.workflow_id,
            desc_err,
            progress_err,
        )
        return None

    return _derive_status(progress, desc)
