"""Application service for workflow-tracking.

Two entry points used by the rest of the app:

- `register(...)` — called by start_* routes/services immediately
  after Temporal hands back a workflow id. Inserts a row in
  `active_workflows` so the badge picks it up on the next poll.

- `update_status(...)` — called by every fragment-status route after
  it computes the current status. Diffs against the prior status,
  fires push notifications on the awaiting-action and terminal
  transitions (idempotent via the notified_*_at columns), and stamps
  terminal_at when the workflow first reaches a terminal status.

Both functions swallow errors — the workflow tracker is observability,
not the path that matters. A failure here must NOT break the start or
fragment poll.
"""

from __future__ import annotations

import logging

from prep.workflows.entities import (
    ActiveWorkflow,
    WorkflowType,
    is_action_required,
    is_terminal,
)
from prep.workflows.repo import ActiveWorkflowsRepo

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
