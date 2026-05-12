"""Entity tests for prep.workflows.

Focus: status bucketing helpers + display-label fallbacks. The
classifier sets are the source of truth for the badge UI; tests
pin the expected groupings so a future status-name typo doesn't
silently drop a workflow from the badge.
"""

from __future__ import annotations

from prep.workflows.entities import (
    ACTION_REQUIRED_STATUSES,
    IN_PROGRESS_STATUSES,
    TERMINAL_STATUSES,
    ActiveWorkflow,
    WorkflowType,
    is_action_required,
    is_in_progress,
    is_terminal,
)

# ---- classifier sets ----------------------------------------------------


def test_action_required_includes_awaiting_apply_and_feedback():
    assert "awaiting_apply" in ACTION_REQUIRED_STATUSES
    assert "awaiting_feedback" in ACTION_REQUIRED_STATUSES


def test_terminal_covers_progress_and_temporal_describe_values():
    # Workflow-side terminal values surfaced via progress.status.
    for s in ("done", "failed", "rejected", "gone"):
        assert s in TERMINAL_STATUSES
    # Temporal describe()-derived statuses surfaced when the query
    # handler is gone (routes map describe-status into progress.status).
    for s in ("COMPLETED", "FAILED", "CANCELED", "TERMINATED"):
        assert s in TERMINAL_STATUSES


def test_in_progress_excludes_terminal_and_action_required():
    # Spot-check the bucketing predicates: anything NOT terminal AND
    # NOT awaiting-action should be in-progress, even if the literal
    # status isn't in the IN_PROGRESS_STATUSES set.
    assert is_in_progress("computing")
    assert is_in_progress("applying")
    assert not is_in_progress("done")
    assert not is_in_progress("awaiting_apply")


def test_classifiers_are_mutually_exclusive_for_known_statuses():
    """A single status string should land in at most one bucket."""
    samples = list(ACTION_REQUIRED_STATUSES) + list(TERMINAL_STATUSES) + list(IN_PROGRESS_STATUSES)
    for s in samples:
        flags = (is_action_required(s), is_terminal(s), is_in_progress(s))
        assert sum(flags) == 1, f"status {s!r} lands in {flags}"


# ---- ActiveWorkflow display helpers -------------------------------------


def _wf(status: str = "computing", **kw) -> ActiveWorkflow:
    defaults = dict(
        workflow_id="transform-deck-1-abc123",
        user_login="alice@example.com",
        workflow_type=WorkflowType.TRANSFORM,
        deck_id=1,
        deck_name="go-systems",
        status=status,
        started_at="2026-05-11T12:00:00+00:00",
        url_path="/transform/transform-deck-1-abc123",
    )
    defaults.update(kw)
    return ActiveWorkflow(**defaults)


def test_display_label_prefers_deck_name():
    w = _wf(deck_name="go-systems")
    assert w.display_label == "go-systems"


def test_display_label_falls_back_to_reorganize_for_transform_with_no_deck():
    w = _wf(deck_id=None, deck_name=None)
    assert w.display_label == "reorganize"


def test_display_label_for_other_type_with_no_deck_uses_type_words():
    w = _wf(deck_id=None, deck_name=None, workflow_type=WorkflowType.TRIVIA_GEN)
    assert w.display_label == "trivia gen"


def test_display_status_humanizes_awaiting_apply():
    w = _wf(status="awaiting_apply")
    assert w.display_status == "review"


def test_display_status_passes_through_in_progress():
    assert _wf(status="computing").display_status == "computing"
    assert _wf(status="applying").display_status == "applying"


def test_display_status_maps_temporal_terminal_synonyms():
    assert _wf(status="COMPLETED").display_status == "done"
    assert _wf(status="CANCELED").display_status == "cancelled"


def test_is_action_required_is_propagated_on_entity():
    assert _wf(status="awaiting_apply").is_action_required
    assert not _wf(status="computing").is_action_required


def test_is_terminal_includes_describe_synonyms_on_entity():
    assert _wf(status="COMPLETED").is_terminal
    assert _wf(status="rejected").is_terminal
