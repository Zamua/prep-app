"""Repo tests for prep.workflows against the per-test temp sqlite.

Covers: register (idempotent), get, update_status, set_terminal_at,
mark_notified, list_for_user sort order, cleanup_stale_terminal.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from prep.workflows.entities import WorkflowType
from prep.workflows.repo import ActiveWorkflowsRepo


def _seed(repo: ActiveWorkflowsRepo, **kw) -> str:
    defaults = dict(
        workflow_id="transform-deck-1-abc123",
        user_login="alice@example.com",
        workflow_type=WorkflowType.TRANSFORM,
        deck_id=1,
        deck_name="go-systems",
        url_path="/transform/transform-deck-1-abc123",
    )
    defaults.update(kw)
    repo.register(**defaults)
    return defaults["workflow_id"]


def test_register_then_get_round_trips(initialized_db: str):
    repo = ActiveWorkflowsRepo()
    wid = _seed(repo, user_login=initialized_db)
    got = repo.get(wid)
    assert got is not None
    assert got.workflow_id == wid
    assert got.workflow_type is WorkflowType.TRANSFORM
    assert got.deck_name == "go-systems"
    assert got.status == "computing"
    assert got.terminal_at is None


def test_register_is_idempotent_on_workflow_id(initialized_db: str):
    """INSERT OR IGNORE on PK conflict — re-registering doesn't error
    and doesn't overwrite the existing row."""
    repo = ActiveWorkflowsRepo()
    _seed(repo, user_login=initialized_db, deck_name="first")
    _seed(repo, user_login=initialized_db, deck_name="second")
    got = repo.get("transform-deck-1-abc123")
    # Original row's deck_name wins — second register is a no-op.
    assert got is not None
    assert got.deck_name == "first"


def test_update_status_persists(initialized_db: str):
    repo = ActiveWorkflowsRepo()
    wid = _seed(repo, user_login=initialized_db)
    repo.update_status(wid, "awaiting_apply")
    got = repo.get(wid)
    assert got is not None
    assert got.status == "awaiting_apply"


def test_set_terminal_at_only_writes_once(initialized_db: str):
    """We want the FIRST terminal timestamp, not the latest poll's —
    re-calling set_terminal_at on a row that already has it leaves
    the original value alone."""
    repo = ActiveWorkflowsRepo()
    wid = _seed(repo, user_login=initialized_db)
    repo.set_terminal_at(wid, terminal_at="2026-05-11T10:00:00+00:00")
    repo.set_terminal_at(wid, terminal_at="2026-05-11T11:00:00+00:00")
    got = repo.get(wid)
    assert got is not None
    assert got.terminal_at == "2026-05-11T10:00:00+00:00"


def test_mark_notified_action_and_terminal(initialized_db: str):
    repo = ActiveWorkflowsRepo()
    wid = _seed(repo, user_login=initialized_db)
    assert repo.get(wid).notified_action_at is None
    repo.mark_notified(wid, "action")
    assert repo.get(wid).notified_action_at is not None
    assert repo.get(wid).notified_terminal_at is None
    repo.mark_notified(wid, "terminal")
    assert repo.get(wid).notified_terminal_at is not None


def test_mark_notified_is_one_shot(initialized_db: str):
    """Re-calling mark_notified with the same kind keeps the original
    timestamp — idempotency for the dedup-on-repeated-polls case."""
    repo = ActiveWorkflowsRepo()
    wid = _seed(repo, user_login=initialized_db)
    repo.mark_notified(wid, "action")
    first_ts = repo.get(wid).notified_action_at
    # Sleep just enough so a re-mark would produce a different ISO
    # timestamp if it were going through.
    time.sleep(0.01)
    repo.mark_notified(wid, "action")
    assert repo.get(wid).notified_action_at == first_ts


def test_list_for_user_filters_by_user(initialized_db: str):
    repo = ActiveWorkflowsRepo()
    from prep.auth.repo import UserRepo

    UserRepo().upsert("bob@example.com", display_name="Bob")
    _seed(repo, workflow_id="w-alice", user_login=initialized_db)
    _seed(repo, workflow_id="w-bob", user_login="bob@example.com")

    alice = repo.list_for_user(initialized_db)
    assert [w.workflow_id for w in alice] == ["w-alice"]
    bob = repo.list_for_user("bob@example.com")
    assert [w.workflow_id for w in bob] == ["w-bob"]


def test_list_for_user_sorts_action_first_then_in_progress_then_terminal(
    initialized_db: str,
):
    """Bucket order is the badge popover's visual hierarchy: urgent
    items at the top, just-completed at the bottom."""
    repo = ActiveWorkflowsRepo()
    _seed(repo, workflow_id="a-inprog", user_login=initialized_db)
    _seed(repo, workflow_id="b-action", user_login=initialized_db)
    _seed(repo, workflow_id="c-done", user_login=initialized_db)
    repo.update_status("b-action", "awaiting_apply")
    repo.update_status("c-done", "done")
    repo.set_terminal_at("c-done")

    items = repo.list_for_user(initialized_db)
    assert [w.workflow_id for w in items] == ["b-action", "a-inprog", "c-done"]


def test_list_for_user_drops_stale_terminal_outside_window(initialized_db: str):
    """Terminal rows older than the recent-window are NOT returned —
    the badge would otherwise keep showing a green check forever."""
    repo = ActiveWorkflowsRepo()
    wid = _seed(repo, user_login=initialized_db)
    repo.update_status(wid, "done")
    # Stamp terminal_at well outside the window.
    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    repo.set_terminal_at(wid, terminal_at=long_ago)

    items = repo.list_for_user(initialized_db)
    assert items == []


def test_cleanup_deletes_only_stale_terminal_rows(initialized_db: str):
    repo = ActiveWorkflowsRepo()
    _seed(repo, workflow_id="inflight", user_login=initialized_db)
    _seed(repo, workflow_id="recent-terminal", user_login=initialized_db)
    _seed(repo, workflow_id="stale-terminal", user_login=initialized_db)

    repo.update_status("recent-terminal", "done")
    repo.set_terminal_at("recent-terminal")  # ~now
    repo.update_status("stale-terminal", "done")
    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    repo.set_terminal_at("stale-terminal", terminal_at=long_ago)

    n = repo.cleanup_stale_terminal()
    assert n == 1
    assert repo.get("stale-terminal") is None
    # In-flight + recent rows still present.
    assert repo.get("inflight") is not None
    assert repo.get("recent-terminal") is not None


def test_list_non_terminal_returns_only_in_flight_rows(initialized_db: str):
    """The reconciler's input set — every row whose terminal_at is NULL,
    across users. Terminal rows are excluded."""
    from prep.auth.repo import UserRepo

    UserRepo().upsert("bob@example.com", display_name="Bob")
    repo = ActiveWorkflowsRepo()
    _seed(repo, workflow_id="alice-inflight", user_login=initialized_db)
    _seed(repo, workflow_id="bob-inflight", user_login="bob@example.com")
    _seed(repo, workflow_id="alice-terminal", user_login=initialized_db)
    repo.update_status("alice-terminal", "done")
    repo.set_terminal_at("alice-terminal")

    items = repo.list_non_terminal()
    ids = {w.workflow_id for w in items}
    assert ids == {"alice-inflight", "bob-inflight"}


def test_list_non_terminal_orders_oldest_first(initialized_db: str):
    """started_at ASC so a backlog gets walked oldest-first per tick —
    stuck workflows surface before fresher ones."""
    repo = ActiveWorkflowsRepo()
    # `register` stamps started_at = now() at insert; force two distinct
    # timestamps by manually rewriting them after registration.
    _seed(repo, workflow_id="newer", user_login=initialized_db)
    _seed(repo, workflow_id="older", user_login=initialized_db)
    from prep.infrastructure.db import cursor

    with cursor() as c:
        c.execute(
            "UPDATE active_workflows SET started_at = ? WHERE workflow_id = ?",
            ("2026-05-10T10:00:00+00:00", "older"),
        )
        c.execute(
            "UPDATE active_workflows SET started_at = ? WHERE workflow_id = ?",
            ("2026-05-11T10:00:00+00:00", "newer"),
        )

    items = repo.list_non_terminal()
    assert [w.workflow_id for w in items] == ["older", "newer"]


def test_prune_terminal_older_than_24h(initialized_db: str):
    """The reconciler's wider prune window — 24h default, not the 60s
    opportunistic-cleanup window used on badge reads."""
    repo = ActiveWorkflowsRepo()
    _seed(repo, workflow_id="recent", user_login=initialized_db)
    _seed(repo, workflow_id="ancient", user_login=initialized_db)
    repo.update_status("recent", "done")
    repo.update_status("ancient", "done")
    repo.set_terminal_at("recent")  # ~now
    twenty_five_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    repo.set_terminal_at("ancient", terminal_at=twenty_five_hours_ago)

    n = repo.prune_terminal_older_than()
    assert n == 1
    assert repo.get("ancient") is None
    assert repo.get("recent") is not None
