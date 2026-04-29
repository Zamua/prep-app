"""Thin wrapper around the Temporal Python SDK for the prep-app.

The Go worker runs the actual workflows. This module is just the *starter*
side — FastAPI calls into it to kick off a GenerateCardsWorkflow, query
its progress, or send it a cancel signal.

We keep the gRPC client as a process-wide singleton (lazy-initialized on
first use) so we don't pay the connection cost on every request.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any

from temporalio.client import Client

TEMPORAL_HOST_PORT = os.environ.get("TEMPORAL_HOST_PORT", "127.0.0.1:7233")
TEMPORAL_NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "prep")
TASK_QUEUE = "prep-generation"
GRADE_WORKFLOW_NAME = "GradeAnswerWorkflow"
TRANSFORM_WORKFLOW_NAME = "TransformWorkflow"
PLAN_GENERATE_WORKFLOW_NAME = "PlanGenerateWorkflow"

_client: Client | None = None


async def _get_client() -> Client:
    global _client
    if _client is None:
        _client = await Client.connect(TEMPORAL_HOST_PORT, namespace=TEMPORAL_NAMESPACE)
    return _client


@dataclass
class StartResult:
    workflow_id: str
    run_id: str


# ---- Grading workflow helpers ----


async def start_grading(
    question_id: int, deck_name: str, user_answer: str, idk: bool, *, user_id: str
) -> StartResult:
    """Start a GradeAnswerWorkflow run. workflow_id encodes deck_name and
    question_id so the polling page (and the eventual result render) can
    parse them back without a side table. user_id is passed through so the
    grading activity scopes its DB reads to the correct user."""
    client = await _get_client()
    wid = f"grade-{deck_name}-q{question_id}-{uuid.uuid4().hex[:10]}"
    handle = await client.start_workflow(
        GRADE_WORKFLOW_NAME,
        {
            "question_id": question_id,
            "user_answer": user_answer,
            "idk": idk,
            "user_id": user_id,
        },
        id=wid,
        task_queue=TASK_QUEUE,
    )
    return StartResult(workflow_id=handle.id, run_id=handle.first_execution_run_id or "")


async def get_grade_progress(workflow_id: str) -> dict[str, Any] | None:
    """Query the grade workflow's getGradeProgress handler. None if the
    workflow has finished (no live handler) — caller should fall back to
    fetching the result via `get_grade_result`."""
    client = await _get_client()
    handle = client.get_workflow_handle(workflow_id)
    try:
        return await handle.query("getGradeProgress")
    except Exception:
        return None


async def get_grade_result(workflow_id: str) -> dict[str, Any] | None:
    """Pull the final GradeAnswerResult from a completed workflow.
    Returns None if the workflow hasn't completed (or failed)."""
    client = await _get_client()
    handle = client.get_workflow_handle(workflow_id)
    try:
        return await handle.result()
    except Exception:
        return None


# ---- Transform workflow helpers ----


async def start_transform(*, user_id: str, scope: str, target_id: int, prompt: str) -> StartResult:
    """Start a TransformWorkflow run. scope is 'card' (target_id = qid)
    or 'deck' (target_id = deck_id). Card scope auto-applies; deck scope
    waits for an apply/reject signal before writing."""
    if scope not in ("card", "deck"):
        raise ValueError(f"unknown transform scope {scope!r}")
    client = await _get_client()
    wid = f"transform-{scope}-{target_id}-{uuid.uuid4().hex[:10]}"
    handle = await client.start_workflow(
        TRANSFORM_WORKFLOW_NAME,
        {
            "user_id": user_id,
            "scope": scope,
            "target_id": target_id,
            "prompt": prompt,
        },
        id=wid,
        task_queue=TASK_QUEUE,
    )
    return StartResult(workflow_id=handle.id, run_id=handle.first_execution_run_id or "")


async def get_transform_progress(workflow_id: str) -> dict[str, Any] | None:
    """Query the transform workflow's getTransformProgress handler.
    Returns the latest TransformProgress dict; None if the workflow has
    finished and there's no live handler — caller can fall back to
    `get_transform_result`."""
    client = await _get_client()
    handle = client.get_workflow_handle(workflow_id)
    try:
        return await handle.query("getTransformProgress")
    except Exception:
        return None


async def get_transform_result(workflow_id: str) -> dict[str, Any] | None:
    client = await _get_client()
    handle = client.get_workflow_handle(workflow_id)
    try:
        return await handle.result()
    except Exception:
        return None


async def signal_apply_transform(workflow_id: str) -> None:
    client = await _get_client()
    handle = client.get_workflow_handle(workflow_id)
    await handle.signal("applyTransform")


async def signal_reject_transform(workflow_id: str) -> None:
    client = await _get_client()
    handle = client.get_workflow_handle(workflow_id)
    await handle.signal("rejectTransform")


# ---- Plan-first generation workflow ---------------------------------------


async def start_plan_generate(
    *, user_id: str, deck_id: int, deck_name: str, prompt: str
) -> StartResult:
    """Start a PlanGenerateWorkflow. Workflow plans cards (claude returns a
    brief outline), waits for accept/reject/feedback signals, then expands
    + inserts cards in parallel on accept."""
    client = await _get_client()
    wid = f"plan-{deck_name}-{uuid.uuid4().hex[:10]}"
    handle = await client.start_workflow(
        PLAN_GENERATE_WORKFLOW_NAME,
        {
            "user_id": user_id,
            "deck_id": deck_id,
            "deck_name": deck_name,
            "prompt": prompt,
        },
        id=wid,
        task_queue=TASK_QUEUE,
    )
    return StartResult(workflow_id=handle.id, run_id=handle.first_execution_run_id or "")


async def get_plan_progress(workflow_id: str) -> dict[str, Any] | None:
    """Query the plan workflow for current state. Returns the
    PlanGenerateProgress dict; None if the workflow has completed and
    the query handler is gone."""
    client = await _get_client()
    handle = client.get_workflow_handle(workflow_id)
    try:
        return await handle.query("getPlanProgress")
    except Exception:
        return None


async def signal_plan_feedback(workflow_id: str, feedback: str) -> None:
    client = await _get_client()
    handle = client.get_workflow_handle(workflow_id)
    await handle.signal("planFeedback", feedback)


async def signal_plan_accept(workflow_id: str) -> None:
    client = await _get_client()
    handle = client.get_workflow_handle(workflow_id)
    await handle.signal("planAccept")


async def signal_plan_reject(workflow_id: str) -> None:
    client = await _get_client()
    handle = client.get_workflow_handle(workflow_id)
    await handle.signal("planReject")
