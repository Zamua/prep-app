"""Service tests for prep.workflows.

Covers: register happy path, update_status diff logic, notification
idempotency (no duplicate pushes on repeated polls), terminal stamping,
and the user-chose-this-terminal-state guard that suppresses the
terminal push when an awaiting-action push already fired.

The push notifier is injected as a stub so we don't drag in pywebpush.
"""

from __future__ import annotations

from prep.workflows import service as workflows_service
from prep.workflows.entities import WorkflowType
from prep.workflows.repo import ActiveWorkflowsRepo


class _StubNotifier:
    """Capturing fake. Drop-in for prep.notify.push.send_to_user."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, user_login, title, body, url=None, source=None, tag=None):
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


def _register(initialized_db: str, **kw) -> str:
    defaults = dict(
        user_login=initialized_db,
        workflow_id="transform-deck-1-abc123",
        workflow_type=WorkflowType.TRANSFORM,
        deck_id=1,
        deck_name="go-systems",
        url_path="/transform/transform-deck-1-abc123",
    )
    defaults.update(kw)
    workflows_service.register(**defaults)
    return defaults["workflow_id"]


def test_register_inserts_row(initialized_db: str):
    wid = _register(initialized_db)
    got = ActiveWorkflowsRepo().get(wid)
    assert got is not None
    assert got.status == "computing"


def test_update_status_to_awaiting_apply_fires_action_push(initialized_db: str):
    wid = _register(initialized_db)
    notifier = _StubNotifier()
    got = workflows_service.update_status(
        workflow_id=wid, new_status="awaiting_apply", notifier=notifier
    )
    assert got is not None
    assert got.status == "awaiting_apply"
    assert len(notifier.calls) == 1
    call = notifier.calls[0]
    assert call["user_login"] == initialized_db
    assert "review" in call["body"].lower() or "ready" in call["body"].lower()
    assert call["url"] == "/transform/transform-deck-1-abc123"
    # mark_notified was stamped so a re-poll won't re-fire.
    assert ActiveWorkflowsRepo().get(wid).notified_action_at is not None


def test_action_push_is_idempotent_on_repeated_polls(initialized_db: str):
    """Two fragment polls landing on awaiting_apply back-to-back must
    NOT result in two pushes."""
    wid = _register(initialized_db)
    notifier = _StubNotifier()
    workflows_service.update_status(workflow_id=wid, new_status="awaiting_apply", notifier=notifier)
    # Simulate a second poll seeing the same status.
    workflows_service.update_status(workflow_id=wid, new_status="awaiting_apply", notifier=notifier)
    assert len(notifier.calls) == 1


def test_terminal_push_fires_when_no_awaiting_action_happened(initialized_db: str):
    """Trivia-gen + grading + plan-reject paths never hit awaiting_apply
    — those terminal transitions should ping the user."""
    wid = _register(
        initialized_db,
        workflow_id="trivia-go-abc123",
        workflow_type=WorkflowType.TRIVIA_GEN,
        url_path="/deck/go-systems",
    )
    notifier = _StubNotifier()
    got = workflows_service.update_status(workflow_id=wid, new_status="done", notifier=notifier)
    assert got is not None
    assert got.terminal_at is not None
    assert len(notifier.calls) == 1
    assert notifier.calls[0]["url"] == "/deck/go-systems"


def test_terminal_push_suppressed_after_awaiting_action(initialized_db: str):
    """The user already saw a push asking them to review; the follow-up
    apply→done is THEIR decision — don't double-ping them."""
    wid = _register(initialized_db)
    notifier = _StubNotifier()
    # 1. computing → awaiting_apply (fires action push)
    workflows_service.update_status(workflow_id=wid, new_status="awaiting_apply", notifier=notifier)
    # 2. user clicks Apply → applying (no push: it's in-progress)
    workflows_service.update_status(workflow_id=wid, new_status="applying", notifier=notifier)
    # 3. applying → done (terminal — but the action push already fired,
    # so we suppress the terminal push)
    workflows_service.update_status(workflow_id=wid, new_status="done", notifier=notifier)
    # Still just the one action push.
    assert len(notifier.calls) == 1
    # terminal_at IS stamped — we just suppressed the push.
    assert ActiveWorkflowsRepo().get(wid).terminal_at is not None


def test_terminal_push_idempotent_on_repeated_polls(initialized_db: str):
    """Same as the action case — repeated polls of a terminal state
    must not duplicate the push."""
    wid = _register(
        initialized_db,
        workflow_id="grade-go-q5-abc123",
        workflow_type=WorkflowType.GRADING,
        url_path="/grading/grade-go-q5-abc123",
    )
    notifier = _StubNotifier()
    workflows_service.update_status(workflow_id=wid, new_status="done", notifier=notifier)
    workflows_service.update_status(workflow_id=wid, new_status="done", notifier=notifier)
    assert len(notifier.calls) == 1


def test_no_push_for_in_progress_transitions(initialized_db: str):
    """Status moving between in-progress states (computing → applying)
    is observational only — no notification."""
    wid = _register(initialized_db)
    notifier = _StubNotifier()
    workflows_service.update_status(workflow_id=wid, new_status="applying", notifier=notifier)
    assert notifier.calls == []


def test_update_status_returns_none_for_unknown_workflow(initialized_db: str):
    """Workflows started before this feature shipped (or any wid we
    don't track) should be a silent no-op — never raise."""
    notifier = _StubNotifier()
    result = workflows_service.update_status(
        workflow_id="ghost-wid", new_status="done", notifier=notifier
    )
    assert result is None
    assert notifier.calls == []


def test_no_op_when_status_unchanged(initialized_db: str):
    """Re-poll with the same status as before — short-circuit, no
    notification path runs."""
    wid = _register(initialized_db)
    notifier = _StubNotifier()
    # Initial status is 'computing'; re-affirm it.
    workflows_service.update_status(workflow_id=wid, new_status="computing", notifier=notifier)
    assert notifier.calls == []


def test_register_swallows_errors(monkeypatch):
    """Register MUST NOT raise — a tracking failure can't be allowed
    to bubble into the workflow-start route."""
    from prep.workflows import repo as repo_mod

    class _Boom:
        def register(self, **kw):
            raise RuntimeError("disk on fire")

    monkeypatch.setattr(repo_mod, "ActiveWorkflowsRepo", lambda: _Boom())
    # Should silently absorb the error.
    workflows_service.register(
        user_login="alice@example.com",
        workflow_id="x",
        workflow_type=WorkflowType.TRANSFORM,
        deck_id=None,
        deck_name=None,
        url_path="/x",
    )


def test_update_status_swallows_errors(monkeypatch):
    from prep.workflows import service as svc_mod

    class _Boom:
        def get(self, wid):
            raise RuntimeError("disk on fire")

    monkeypatch.setattr(svc_mod, "ActiveWorkflowsRepo", lambda: _Boom())
    result = svc_mod.update_status(workflow_id="x", new_status="done")
    assert result is None
