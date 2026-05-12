"""Entities for the workflow-tracking bounded context.

`ActiveWorkflow` is the typed view over a row in the
`active_workflows` table. One row per (user, in-flight workflow).
Terminal rows are kept briefly (60s) so the badge can render a green
"✅ just-completed" pill before the row is cleaned up.

The status string is workflow-type-specific (transform has its own
states, plan has its own, etc.) — we don't try to normalize them
into a single enum. Instead, three sets classify a status:

- `ACTION_REQUIRED_STATUSES` — awaiting user input (popover sorts to top)
- `TERMINAL_STATUSES`        — workflow is done (green check; cleanup eligible)
- `IN_PROGRESS_STATUSES`     — everything else (blue spinner)

The classifier functions below are the single source of truth for the
badge UI's bucketing.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class WorkflowType(str, Enum):
    """Which Temporal workflow this row represents.

    Each value maps to one of the four workflow families the worker
    runs (see `prep.temporal_client` + `worker-go/workflows/`). The
    UI uses this to pick a type-icon + a short label in the popover.
    """

    TRANSFORM = "transform"
    PLAN = "plan"
    TRIVIA_GEN = "trivia_gen"
    GRADING = "grading"

    def __str__(self) -> str:
        return self.value


# The status strings observed in `progress.status` for each workflow
# family. Drawn from worker-go/workflows/*.go and verified against the
# fragment endpoints. We deliberately use the union of all observed
# values across types so the classifier sets stay small and explicit.

ACTION_REQUIRED_STATUSES = frozenset(
    {
        "awaiting_apply",  # transform: plan ready, user must apply/reject
        "awaiting_feedback",  # plan: outline ready, user must accept/reject/feedback
    }
)

TERMINAL_STATUSES = frozenset(
    {
        "done",
        "failed",
        "rejected",
        "gone",
        # Temporal's describe() statuses surface here when the query
        # handler is gone and the route maps describe-status into
        # progress.status — see transform_view / plan_view code paths.
        "COMPLETED",
        "FAILED",
        "CANCELED",
        "TERMINATED",
    }
)

IN_PROGRESS_STATUSES = frozenset(
    {
        "computing",
        "planning",
        "generating",
        "grading",
        "applying",
        "rejecting",
        "accepting",
        "asking_claude",  # trivia gen
        "inserting",  # trivia gen
    }
)


def is_action_required(status: str) -> bool:
    return status in ACTION_REQUIRED_STATUSES


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


def is_in_progress(status: str) -> bool:
    """Anything NOT terminal and NOT awaiting-action. We don't strictly
    list every in-progress status (workflows occasionally surface
    transient values like '' or 'starting…'); treat anything not in
    the other two buckets as in-progress for UI purposes."""
    return not is_terminal(status) and not is_action_required(status)


class ActiveWorkflow(BaseModel):
    """A single in-flight (or recently-terminal) workflow tracked for
    the masthead badge.

    `terminal_at` is the timestamp the workflow first reached a
    terminal status — used by the badge to render a brief "just done"
    pill and by cleanup to age rows out after ~60s.

    `notified_action_at` + `notified_terminal_at` are idempotency
    guards so the service doesn't fire duplicate push notifications
    on repeated polls of the same status.
    """

    workflow_id: str = Field(min_length=1, max_length=200)
    user_login: str = Field(min_length=1, max_length=255)
    workflow_type: WorkflowType
    deck_id: int | None = None
    deck_name: str | None = None
    status: str = Field(min_length=0, max_length=64)
    started_at: str
    terminal_at: str | None = None
    url_path: str = Field(min_length=1, max_length=512)
    notified_action_at: str | None = None
    notified_terminal_at: str | None = None

    # ---- UI bucketing helpers (templates call these via dot-access) ----

    @property
    def is_action_required(self) -> bool:
        return is_action_required(self.status)

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self.status)

    @property
    def is_in_progress(self) -> bool:
        return is_in_progress(self.status)

    @property
    def display_status(self) -> str:
        """Human-readable status label for the popover row.

        Keep this short — the popover is narrow. Type-specific
        wording where it adds clarity (e.g. 'review' for awaiting_apply
        reads better than 'awaiting apply')."""
        s = self.status
        if s in ("awaiting_apply",):
            return "review"
        if s in ("awaiting_feedback",):
            return "review plan"
        if s in ("done", "COMPLETED"):
            return "done"
        if s in ("failed", "FAILED"):
            return "failed"
        if s in ("rejected", "CANCELED", "TERMINATED", "gone"):
            return "cancelled"
        if s == "asking_claude":
            return "asking claude"
        # Default: surface the raw status (computing, applying, etc.)
        return s or "starting"

    @property
    def display_label(self) -> str:
        """The primary label shown in the popover row — deck name when
        we have one, otherwise the workflow type. Reorganize is the
        notable cross-deck case where deck_name is None."""
        if self.deck_name:
            return self.deck_name
        if self.workflow_type is WorkflowType.TRANSFORM:
            return "reorganize"
        return self.workflow_type.value.replace("_", " ")
