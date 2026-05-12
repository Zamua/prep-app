"""HTTP routes for the workflows bounded context.

Single endpoint today: `GET /api/active-workflows-badge`. Returns the
masthead badge fragment (htmx-friendly). The fragment is empty when
the user has no active workflows so an htmx swap is a no-op.

The base masthead in `templates/base.html` includes a placeholder
`<div>` that htmx polls every 5s; the response replaces the
placeholder with either an empty div (no badge) or the full
`<details>` popover.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from prep.auth import current_user
from prep.web.templates import templates
from prep.workflows.repo import ActiveWorkflowsRepo

router = APIRouter()


@router.get("/api/active-workflows-badge", response_class=HTMLResponse)
def active_workflows_badge(
    request: Request,
    user: dict = Depends(current_user),
):
    """Render the htmx-polled badge fragment.

    Opportunistic cleanup of stale terminal rows happens here so the
    badge never carries indefinite cruft. We do it on the read path
    rather than via a scheduler task — cheap single-DELETE-per-poll
    is fine at our scale and keeps the scheduling story simple."""
    repo = ActiveWorkflowsRepo()
    repo.cleanup_stale_terminal()
    workflows = repo.list_for_user(user["tailscale_login"])
    return templates.TemplateResponse(
        request,
        "partials/workflow_badge.html",
        {"workflows": workflows, "user": user},
    )
