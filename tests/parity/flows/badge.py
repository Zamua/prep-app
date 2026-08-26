"""The masthead workflow badge with a job actually in flight.

Held at its first step, so the chip's count and the popover row's
status are the same on every run.
"""

import json

from tests.parity.harness.registry import FlowCtx, flow
from tests.parity.harness.steps import (
    click_and_wait,
    open_page,
    shot,
    wait_badge,
    wait_badge_status,
)

PLAN = json.dumps(
    [
        {
            "title": "Cache-oblivious layouts",
            "brief": "Ask what a cache-oblivious algorithm assumes about block size.",
            "type": "short",
            "topic": "memory",
        }
    ]
)


@flow("badge", phase=4, seed="workflows", covers=("partials/workflow_badge.html", "index.html"))
def badge(ctx: FlowCtx) -> None:
    page = ctx.page
    llm = ctx.llm

    llm.control.canned(PLAN)
    llm.hold()
    page.goto(ctx.url("/decks/new/srs"), wait_until="load")
    page.fill("input[name=name]", "Memory hierarchy")
    page.fill("textarea[name=context_prompt]", "Caches, locality and cache-oblivious algorithms.")
    with page.expect_navigation(wait_until="load"):
        page.click("button[value=plan]")
    page.wait_for_selector("#plan-progress")
    # Only the plan fragment's own poll writes the tracked status, so let
    # it land before leaving: the badge would otherwise still carry the
    # status the start registered.
    wait_badge_status(ctx, "planning")

    open_page(ctx, "/", ".decks-section, .empty-state")
    wait_badge(ctx, "active workflows (1 of 1)")
    wait_badge_status(ctx, "planning")
    shot(ctx, "chip")

    click_and_wait(ctx, "#workflow-badge summary", "#workflow-badge details[open]")
    shot(ctx, "panel")

    llm.release()
    llm.control.canned(None)
