"""Route tests for prep.workflows.routes.

Covers the /api/active-workflows-badge endpoint across:
- zero state (empty workflow list → response carries data-empty marker)
- one workflow in flight → response contains badge with count
- multiple workflows → ordered popover rows
- htmx vs plain GET (route returns the same fragment either way; htmx
  wiring is in the fragment itself, not in negotiation)
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from prep.workflows.entities import WorkflowType
from prep.workflows.repo import ActiveWorkflowsRepo


def test_badge_empty_when_no_workflows(client: TestClient, initialized_db: str):
    r = client.get("/api/active-workflows-badge")
    assert r.status_code == 200
    # The wrapper renders with data-empty="1" and no popover.
    assert 'data-empty="1"' in r.text
    assert "<details" not in r.text


def test_badge_renders_count_for_one_workflow(client: TestClient, initialized_db: str):
    ActiveWorkflowsRepo().register(
        workflow_id="transform-deck-1-abc123",
        user_login=initialized_db,
        workflow_type=WorkflowType.TRANSFORM,
        deck_id=1,
        deck_name="go-systems",
        url_path="/transform/transform-deck-1-abc123",
    )
    r = client.get("/api/active-workflows-badge")
    assert r.status_code == 200
    assert "data-empty" not in r.text
    # The chip count + the row deck name should both render.
    assert ">1<" in r.text  # count
    assert "go-systems" in r.text
    # The "view" link points at the workflow's url_path.
    assert "/transform/transform-deck-1-abc123" in r.text


def test_badge_renders_multiple_workflows_sorted_by_urgency(
    client: TestClient, initialized_db: str
):
    repo = ActiveWorkflowsRepo()
    repo.register(
        workflow_id="w-action",
        user_login=initialized_db,
        workflow_type=WorkflowType.TRANSFORM,
        deck_id=1,
        deck_name="urgent-deck",
        url_path="/transform/w-action",
    )
    repo.register(
        workflow_id="w-inprog",
        user_login=initialized_db,
        workflow_type=WorkflowType.PLAN,
        deck_id=2,
        deck_name="middle-deck",
        url_path="/plan/w-inprog",
    )
    repo.update_status("w-action", "awaiting_apply")
    r = client.get("/api/active-workflows-badge")
    text = r.text
    assert r.status_code == 200
    # Urgent deck should appear before middle-deck in the rendered HTML.
    assert text.index("urgent-deck") < text.index("middle-deck")


def test_badge_filters_by_user(client: TestClient, initialized_db: str):
    """The route reads user from the auth dependency; bob's workflows
    don't show up for alice."""
    from prep.auth.repo import UserRepo

    UserRepo().upsert("bob@example.com", display_name="Bob")
    ActiveWorkflowsRepo().register(
        workflow_id="bob-wf",
        user_login="bob@example.com",
        workflow_type=WorkflowType.TRANSFORM,
        deck_id=99,
        deck_name="bobs-deck",
        url_path="/transform/bob-wf",
    )
    # TestClient is auth'd as the env default user (alice).
    r = client.get("/api/active-workflows-badge")
    assert "bobs-deck" not in r.text


def test_badge_handles_hx_header_same_as_plain_get(client: TestClient, initialized_db: str):
    """The fragment is the same whether or not the request carried
    HX-Request: true. Pinning this so a future contributor doesn't
    accidentally branch on the header — the htmx wiring lives in the
    fragment, not at the route level."""
    ActiveWorkflowsRepo().register(
        workflow_id="w1",
        user_login=initialized_db,
        workflow_type=WorkflowType.TRANSFORM,
        deck_id=1,
        deck_name="deck-a",
        url_path="/transform/w1",
    )
    plain = client.get("/api/active-workflows-badge").text
    hx = client.get("/api/active-workflows-badge", headers={"HX-Request": "true"}).text
    assert plain == hx


def test_badge_response_includes_polling_attributes(client: TestClient, initialized_db: str):
    """The wrapper element carries hx-get + hx-trigger so the swap
    response continues polling without server-side help."""
    r = client.get("/api/active-workflows-badge")
    assert r.status_code == 200
    assert 'hx-get="/api/active-workflows-badge"' in r.text
    assert 'hx-trigger="every 5s"' in r.text


def test_badge_swaps_to_done_state_when_all_terminal(client: TestClient, initialized_db: str):
    """When every row in the popover is terminal (rejected/done/failed/
    cancelled), the chip should drop the spinner and the popover head
    should read 'recently completed' instead of lying about 'active
    operations'."""
    repo = ActiveWorkflowsRepo()
    repo.register(
        workflow_id="t-done",
        user_login=initialized_db,
        workflow_type=WorkflowType.TRANSFORM,
        deck_id=1,
        deck_name="go-systems",
        url_path="/transform/t-done",
    )
    repo.update_status("t-done", "rejected")
    repo.register(
        workflow_id="p-done",
        user_login=initialized_db,
        workflow_type=WorkflowType.PLAN,
        deck_id=1,
        deck_name="go-systems",
        url_path="/plan/p-done",
    )
    repo.update_status("p-done", "rejected")

    r = client.get("/api/active-workflows-badge")
    assert r.status_code == 200
    body = r.text
    # The chip carries a marker class so CSS can drop the spinner.
    assert "workflow-indicator--done" in body
    # Header copy switches.
    assert "recently completed" in body
    assert "active operation" not in body


def test_badge_mixed_active_and_terminal(client: TestClient, initialized_db: str):
    """One in-progress + one just-terminal — the header should
    differentiate, and the spinner should keep running because work
    is still active."""
    repo = ActiveWorkflowsRepo()
    repo.register(
        workflow_id="t-running",
        user_login=initialized_db,
        workflow_type=WorkflowType.TRANSFORM,
        deck_id=1,
        deck_name="d1",
        url_path="/transform/t-running",
    )
    repo.update_status("t-running", "computing")
    repo.register(
        workflow_id="p-done",
        user_login=initialized_db,
        workflow_type=WorkflowType.PLAN,
        deck_id=1,
        deck_name="d1",
        url_path="/plan/p-done",
    )
    repo.update_status("p-done", "done")

    r = client.get("/api/active-workflows-badge")
    assert r.status_code == 200
    body = r.text
    # Mixed state → spinner stays.
    assert "workflow-indicator--done" not in body
    # Header surfaces both counts.
    assert "1 active" in body
    assert "1 just done" in body


def test_badge_cleanup_drops_stale_terminal_on_read(client: TestClient, initialized_db: str):
    """Hitting the badge route opportunistically prunes long-stale
    terminal rows. The visible workflow count stays accurate."""
    from datetime import datetime, timedelta, timezone

    repo = ActiveWorkflowsRepo()
    repo.register(
        workflow_id="stale",
        user_login=initialized_db,
        workflow_type=WorkflowType.TRANSFORM,
        deck_id=1,
        deck_name="old-deck",
        url_path="/transform/stale",
    )
    repo.update_status("stale", "done")
    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    repo.set_terminal_at("stale", terminal_at=long_ago)
    # Pre-condition: the row exists.
    assert repo.get("stale") is not None
    r = client.get("/api/active-workflows-badge")
    assert r.status_code == 200
    # Cleanup should have run; the stale row is gone.
    assert repo.get("stale") is None
