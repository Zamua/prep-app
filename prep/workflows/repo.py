"""Repository for the active_workflows table.

SQL lives here directly. The repo returns `ActiveWorkflow` entities;
the service layer above orchestrates inserts/updates + push fan-out.

Cleanup policy:

- Rows with `terminal_at` older than RECENT_TERMINAL_WINDOW are pruned
  on every badge fetch (opportunistic — keeps the badge popover clean
  even between reconciler ticks). Cheap single-DELETE-per-poll.
- Rows with `terminal_at` older than ~24h are pruned by the periodic
  workflow reconciler (`prep.workflows.scheduler`). The opportunistic
  badge cleanup uses the short 60s window; the reconciler's wider
  window catches rows for users who haven't visited recently.

Rows for in-flight workflows are never auto-pruned. The reconciler
re-queries Temporal for any non-terminal row, transitions it forward
through `service.update_status`, and stamps terminal_at when the
workflow reaches a terminal state.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from prep.infrastructure.db import cursor, now
from prep.workflows.entities import ActiveWorkflow, WorkflowType

# How long after reaching a terminal status the row stays visible in
# the badge as a "✅ just-done" pill before it's pruned. 60s matches
# the spec — long enough for the user to notice, short enough that
# the badge clears on its own without manual dismissal.
RECENT_TERMINAL_WINDOW_SECONDS = 60

# Wider window the periodic reconciler uses to hard-delete long-since-
# terminal rows. Set to 24h so a user who took a few hours to come back
# still sees their recent-completion pill (within the 60s opportunistic
# window after their next badge fetch, which fires set_terminal_at on
# transition), but the table doesn't accumulate forever for users who
# never come back.
RECONCILER_PRUNE_WINDOW_SECONDS = 24 * 60 * 60


def _row_to_entity(row) -> ActiveWorkflow:
    return ActiveWorkflow(
        workflow_id=row["workflow_id"],
        user_login=row["user_login"],
        workflow_type=WorkflowType(row["workflow_type"]),
        deck_id=row["deck_id"],
        deck_name=row["deck_name"],
        status=row["status"] or "",
        started_at=row["started_at"],
        terminal_at=row["terminal_at"],
        url_path=row["url_path"],
        notified_action_at=row["notified_action_at"],
        notified_terminal_at=row["notified_terminal_at"],
    )


class ActiveWorkflowsRepo:
    """Read/write access to the `active_workflows` table."""

    def register(
        self,
        *,
        workflow_id: str,
        user_login: str,
        workflow_type: WorkflowType,
        deck_id: int | None,
        deck_name: str | None,
        url_path: str,
        initial_status: str = "computing",
    ) -> None:
        """Insert a fresh row for a just-started workflow.

        Idempotent on workflow_id (INSERT OR IGNORE) so a re-trigger
        of the start route doesn't error — the existing row's status
        will continue to be updated as fragment polls arrive."""
        ts = now()
        with cursor() as c:
            c.execute(
                """INSERT OR IGNORE INTO active_workflows
                   (workflow_id, user_login, workflow_type, deck_id, deck_name,
                    status, started_at, url_path)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    workflow_id,
                    user_login,
                    str(workflow_type),
                    deck_id,
                    deck_name,
                    initial_status,
                    ts,
                    url_path,
                ),
            )

    def get(self, workflow_id: str) -> ActiveWorkflow | None:
        with cursor() as c:
            row = c.execute(
                "SELECT * FROM active_workflows WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
        return _row_to_entity(row) if row else None

    def update_status(self, workflow_id: str, status: str) -> None:
        with cursor() as c:
            c.execute(
                "UPDATE active_workflows SET status = ? WHERE workflow_id = ?",
                (status, workflow_id),
            )

    def set_terminal_at(self, workflow_id: str, terminal_at: str | None = None) -> None:
        """Stamp the row's terminal_at column (idempotent — only writes
        if currently NULL, so we capture the FIRST time the workflow
        went terminal even if subsequent polls keep landing on the same
        terminal state)."""
        ts = terminal_at or now()
        with cursor() as c:
            c.execute(
                "UPDATE active_workflows SET terminal_at = ? "
                "WHERE workflow_id = ? AND terminal_at IS NULL",
                (ts, workflow_id),
            )

    def mark_notified(self, workflow_id: str, kind: str) -> None:
        """Stamp the appropriate notified_*_at column so the service
        doesn't re-fire a push for the same transition on subsequent
        polls. `kind` is 'action' or 'terminal'."""
        if kind not in ("action", "terminal"):
            raise ValueError(f"unknown notification kind: {kind!r}")
        col = "notified_action_at" if kind == "action" else "notified_terminal_at"
        ts = now()
        with cursor() as c:
            c.execute(
                f"UPDATE active_workflows SET {col} = ? "
                f"WHERE workflow_id = ? AND {col} IS NULL",
                (ts, workflow_id),
            )

    def list_for_user(
        self,
        user_login: str,
        *,
        recent_terminal_window_seconds: int = RECENT_TERMINAL_WINDOW_SECONDS,
    ) -> list[ActiveWorkflow]:
        """Active + recently-terminal rows for one user, sorted for the
        badge popover:

        - awaiting-action first (most urgent)
        - then in-progress
        - then just-completed (terminal within the window)

        Within each bucket, newest-first by `started_at` (so the most
        recently kicked-off workflow appears at the top of its group).
        """
        cutoff_dt = datetime.now(timezone.utc) - timedelta(seconds=recent_terminal_window_seconds)
        cutoff_iso = cutoff_dt.isoformat()
        with cursor() as c:
            rows = c.execute(
                """SELECT * FROM active_workflows
                   WHERE user_login = ?
                     AND (terminal_at IS NULL OR terminal_at >= ?)
                   ORDER BY started_at DESC""",
                (user_login, cutoff_iso),
            ).fetchall()
        items = [_row_to_entity(r) for r in rows]

        # The SQL ORDER BY already gives us newest-first. Now stable-sort
        # by bucket so awaiting-action floats to the top, in-progress
        # next, then recently-completed — preserving newest-first within
        # each group. Bucket numbers chosen so the natural ASC sort
        # puts the most urgent group first.
        def _bucket(w: ActiveWorkflow) -> int:
            if w.is_action_required:
                return 0
            if w.is_terminal:
                return 2
            return 1

        items.sort(key=_bucket)
        return items

    def cleanup_stale_terminal(
        self,
        *,
        window_seconds: int = RECENT_TERMINAL_WINDOW_SECONDS,
    ) -> int:
        """Delete terminal rows older than the window. Returns the row
        count deleted (mostly useful for tests). Safe to call on every
        badge fetch — it's a single indexed DELETE, cheap at our scale."""
        cutoff_dt = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
        cutoff_iso = cutoff_dt.isoformat()
        with cursor() as c:
            cur = c.execute(
                "DELETE FROM active_workflows WHERE terminal_at IS NOT NULL AND terminal_at < ?",
                (cutoff_iso,),
            )
            return cur.rowcount

    def list_non_terminal(self) -> list[ActiveWorkflow]:
        """All rows that have NOT yet been stamped terminal — i.e. the
        set the reconciler must re-query Temporal for. Cross-user; this
        is the periodic background sweep, not the user-facing badge.

        Ordering is `started_at ASC` so the oldest in-flight workflows
        get re-checked first per tick (they're the most likely to be
        stuck or have just transitioned)."""
        with cursor() as c:
            rows = c.execute(
                "SELECT * FROM active_workflows "
                "WHERE terminal_at IS NULL "
                "ORDER BY started_at ASC"
            ).fetchall()
        return [_row_to_entity(r) for r in rows]

    def prune_terminal_older_than(
        self,
        *,
        window_seconds: int = RECONCILER_PRUNE_WINDOW_SECONDS,
    ) -> int:
        """Hard-delete terminal rows whose terminal_at is more than
        `window_seconds` ago. Used by the reconciler to age out long-
        forgotten rows (24h default) without disturbing the short
        opportunistic-cleanup window that `cleanup_stale_terminal` runs
        on badge reads (60s)."""
        return self.cleanup_stale_terminal(window_seconds=window_seconds)
