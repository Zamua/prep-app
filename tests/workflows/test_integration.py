"""Integration tests for the workflow tracker.

Verify that the workflow registry is updated by the existing route
plumbing — the fragment polling endpoints (which are the canonical
status-watch path) should call workflows.service.update_status on
every poll so the badge sees fresh data without a separate cron tick.

We don't drive the real Temporal worker; tests stub the temporal
client surface at the module level so the route can run end-to-end
through our HTTP test client.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from prep.workflows.entities import WorkflowType
from prep.workflows.repo import ActiveWorkflowsRepo


class _FakeTemporal:
    """Stand-in for prep.temporal_client. Only implements what the
    fragment polling routes call: get_*_progress + describe_workflow.

    Pass `progress_status` to control what the route sees."""

    def __init__(self, progress_status: str = "computing"):
        self.progress_status = progress_status

    async def get_transform_progress(self, wid: str):
        return {"status": self.progress_status, "plan": {}}

    async def get_plan_progress(self, wid: str):
        return {"status": self.progress_status, "plan": {}}

    async def get_grade_progress(self, wid: str):
        return {"status": self.progress_status}

    async def get_trivia_progress(self, wid: str):
        return {"status": self.progress_status}

    async def describe_workflow(self, wid: str):
        return {"status": "RUNNING"}


def _patch_temporal(monkeypatch, fake):
    """Replace prep.temporal_client lookups with the fake. Routes
    `from prep import temporal_client` then call attributes on it."""
    import prep.temporal_client as tc

    for attr in (
        "get_transform_progress",
        "get_plan_progress",
        "get_grade_progress",
        "get_trivia_progress",
        "describe_workflow",
    ):
        monkeypatch.setattr(tc, attr, getattr(fake, attr))


def test_transform_fragment_updates_workflow_status(
    client: TestClient, initialized_db: str, monkeypatch
):
    """Hitting /transform/{wid}/fragment with a registered workflow row
    should update the row's status from whatever the workflow reports.
    This is the load-bearing assertion for the polling-loop integration."""
    # Pre-condition: seed a deck the user owns so the ownership check
    # passes, and register a tracker row with the matching wid shape.
    from prep.decks.repo import DeckRepo

    deck_id = DeckRepo().create(initialized_db, "go-systems")
    wid = f"transform-deck-{deck_id}-abc1234567"

    repo = ActiveWorkflowsRepo()
    repo.register(
        workflow_id=wid,
        user_login=initialized_db,
        workflow_type=WorkflowType.TRANSFORM,
        deck_id=deck_id,
        deck_name="go-systems",
        url_path=f"/transform/{wid}",
    )
    assert repo.get(wid).status == "computing"

    # Polling sees awaiting_apply → tracker should pick that up.
    _patch_temporal(monkeypatch, _FakeTemporal(progress_status="awaiting_apply"))
    r = client.get(f"/transform/{wid}/fragment")
    assert r.status_code == 200
    assert repo.get(wid).status == "awaiting_apply"


def test_plan_fragment_updates_workflow_status(
    client: TestClient, initialized_db: str, monkeypatch
):
    from prep.decks.repo import DeckRepo

    DeckRepo().create(initialized_db, "designs")
    wid = "plan-designs-abc1234567"
    repo = ActiveWorkflowsRepo()
    repo.register(
        workflow_id=wid,
        user_login=initialized_db,
        workflow_type=WorkflowType.PLAN,
        deck_id=None,
        deck_name="designs",
        url_path=f"/plan/{wid}",
    )

    _patch_temporal(monkeypatch, _FakeTemporal(progress_status="awaiting_feedback"))
    r = client.get(f"/plan/{wid}/fragment")
    assert r.status_code == 200
    assert repo.get(wid).status == "awaiting_feedback"


def test_grading_fragment_updates_workflow_status(
    client: TestClient, initialized_db: str, monkeypatch
):
    """Grading fragment fires its status update before short-circuiting
    on terminal status — so even the redirect path keeps the tracker
    in sync."""
    from prep.decks.entities import NewQuestion, QuestionType
    from prep.decks.repo import DeckRepo, QuestionRepo

    deck_id = DeckRepo().create(initialized_db, "code-1")
    qid = QuestionRepo().add(
        initialized_db,
        deck_id,
        NewQuestion(type=QuestionType.SHORT, prompt="q?", answer="A"),
    )
    wid = f"grade-code-1-q{qid}-abc1234567"
    repo = ActiveWorkflowsRepo()
    repo.register(
        workflow_id=wid,
        user_login=initialized_db,
        workflow_type=WorkflowType.GRADING,
        deck_id=None,
        deck_name="code-1",
        url_path=f"/grading/{wid}",
        initial_status="grading",
    )

    _patch_temporal(monkeypatch, _FakeTemporal(progress_status="done"))
    r = client.get(f"/grading/{wid}/fragment")
    # Terminal grading → HX-Redirect (200 with no body) is the route's
    # expected response.
    assert r.status_code == 200
    assert repo.get(wid).status == "done"
    assert repo.get(wid).terminal_at is not None


def test_trivia_gen_fragment_updates_workflow_status(
    client: TestClient, initialized_db: str, monkeypatch
):
    from prep.decks.repo import DeckRepo

    DeckRepo().create_trivia(initialized_db, "facts", topic="random facts", interval_minutes=30)
    wid = "trivia-facts-abc1234567"
    repo = ActiveWorkflowsRepo()
    repo.register(
        workflow_id=wid,
        user_login=initialized_db,
        workflow_type=WorkflowType.TRIVIA_GEN,
        deck_id=None,
        deck_name="facts",
        url_path=f"/trivia/gen/{wid}",
    )

    _patch_temporal(monkeypatch, _FakeTemporal(progress_status="inserting"))
    r = client.get(f"/trivia/gen/{wid}/fragment")
    assert r.status_code == 200
    assert repo.get(wid).status == "inserting"


def test_fragment_with_no_registered_workflow_is_silent(
    client: TestClient, initialized_db: str, monkeypatch
):
    """Workflows started before the tracker shipped have no row to
    update — the fragment endpoint must not error and the response
    shape must be unchanged."""
    from prep.decks.repo import DeckRepo

    deck_id = DeckRepo().create(initialized_db, "vintage")
    # No registry row — wid is shaped right but the tracker doesn't know it.
    wid = f"transform-deck-{deck_id}-abc1234567"
    _patch_temporal(monkeypatch, _FakeTemporal(progress_status="computing"))
    r = client.get(f"/transform/{wid}/fragment")
    assert r.status_code == 200
