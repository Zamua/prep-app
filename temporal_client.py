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

from temporalio.client import Client, WorkflowHandle

TEMPORAL_HOST_PORT = os.environ.get("TEMPORAL_HOST_PORT", "127.0.0.1:7233")
TEMPORAL_NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "prep")
TASK_QUEUE = "prep-generation"
WORKFLOW_NAME = "GenerateCardsWorkflow"

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


async def start_generation(deck_name: str, count: int) -> StartResult:
    """Start a GenerateCardsWorkflow run. Returns immediately — the workflow
    runs asynchronously on the Go worker."""
    client = await _get_client()
    wid = f"gen-{deck_name}-{uuid.uuid4().hex[:12]}"
    handle = await client.start_workflow(
        WORKFLOW_NAME,
        {"deck_name": deck_name, "count": count},
        id=wid,
        task_queue=TASK_QUEUE,
    )
    return StartResult(workflow_id=handle.id, run_id=handle.first_execution_run_id or "")


async def get_progress(workflow_id: str) -> dict[str, Any] | None:
    """Query the workflow's getProgress handler.

    Returns the progress dict, or None if the workflow has no live handler
    (e.g. it completed and the result is final). Caller should fall back to
    `describe_workflow` to get the final status.
    """
    client = await _get_client()
    handle: WorkflowHandle = client.get_workflow_handle(workflow_id)
    try:
        return await handle.query("getProgress")
    except Exception:
        return None


async def describe_workflow(workflow_id: str) -> dict[str, Any]:
    """Pull the workflow's execution status from Temporal — used to detect
    completed/failed/cancelled runs after the in-memory query handler is gone.
    """
    client = await _get_client()
    handle = client.get_workflow_handle(workflow_id)
    desc = await handle.describe()
    return {
        "status": desc.status.name if desc.status else "UNKNOWN",
        "started_at": desc.start_time.isoformat() if desc.start_time else None,
        "closed_at": desc.close_time.isoformat() if desc.close_time else None,
        "task_queue": desc.task_queue,
    }


async def cancel_generation(workflow_id: str) -> None:
    """Send the cancelGeneration signal — the workflow will exit cleanly
    after the current card finishes."""
    client = await _get_client()
    handle = client.get_workflow_handle(workflow_id)
    await handle.signal("cancelGeneration", "")
