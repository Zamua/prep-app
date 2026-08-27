"""Plan-first generation, every status the partial renders.

The deck slug a plan is started against is minted at random by the
route, so the prompt the worker sends is not stable across runs and no
recorded LLM fixture can key on it. The stub's canned answer stands in
for the agent instead: it is reset between steps, which is what makes
each round's outline, the expansion and the failure deterministic.

`accepting` and `applying` have no shot. Both are written and
overwritten inside one workflow task (accept -> generating; expansion
done -> applying -> the insert activities), so no query can observe
them; only `rejecting`, which the workflow yields on, survives.
"""

import json

from tests.parity.harness.registry import FlowCtx, flow
from tests.parity.harness.steps import (
    freeze_polling,
    fresh_swap,
    resume_polling,
    shot,
    wait_badge,
    wait_status,
)

PLAN_V1 = json.dumps(
    [
        {
            "title": "Average case of quicksort",
            "brief": "Ask for the expected running time on random input.",
            "type": "short",
            "topic": "complexity",
        },
        {
            "title": "Which sorts are stable",
            "brief": "Pick every sort that keeps equal keys in input order.",
            "type": "multi",
            "topic": "sorting",
        },
        {
            "title": "Merge sort's extra space",
            "brief": "One choice among four for the auxiliary space merge sort needs.",
            "type": "mcq",
            "topic": "complexity",
        },
        {
            "title": "Implement insertion sort",
            "brief": "Write insertion sort over a list of integers.",
            "type": "code",
            "topic": "sorting",
            "language": "python",
        },
    ]
)

PLAN_V2 = json.dumps(
    [
        {
            "title": "Average case of quicksort",
            "brief": "Ask for the expected running time on random input.",
            "type": "short",
            "topic": "complexity",
        },
        {
            "title": "Worst case of quicksort",
            "brief": "Ask what input shape degrades quicksort to quadratic.",
            "type": "short",
            "topic": "complexity",
        },
        {
            "title": "Implement insertion sort",
            "brief": "Write insertion sort over a list of integers.",
            "type": "code",
            "topic": "sorting",
            "language": "python",
        },
    ]
)

PLAN_ONE = json.dumps(
    [
        {
            "title": "Lower bound on comparison sorts",
            "brief": "Ask why no comparison sort beats n log n.",
            "type": "short",
            "topic": "complexity",
        }
    ]
)

CARD = json.dumps(
    {
        "type": "short",
        "topic": "complexity",
        "prompt": "Why can no comparison sort do better than O(n log n)?",
        "answer": "A comparison sort's decision tree has n! leaves, so its height is at least log2(n!) = O(n log n).",
        "rubric": "- Names the decision-tree argument\n- Ties n! leaves to the height bound",
    }
)

NOT_JSON = "I could not write that card, sorry."


def _start(ctx: FlowCtx, name: str, description: str) -> None:
    """Fill the new-SRS-deck form and take the plan branch."""
    page = ctx.page
    page.goto(ctx.url("/decks/new/srs"), wait_until="load")
    page.fill("input[name=name]", name)
    page.fill("textarea[name=context_prompt]", description)
    with page.expect_navigation(wait_until="load"):
        page.click("button[value=plan]")
    page.wait_for_selector("#plan-progress")


@flow(
    "plan",
    phase=4,
    seed="workflows",
    covers=(
        "plan.html",
        "partials/plan_progress.html#planning",
        "partials/plan_progress.html#awaiting_feedback",
        "partials/plan_progress.html#replanning",
        "partials/plan_progress.html#generating",
        "partials/plan_progress.html#rejecting",
        "partials/plan_progress.html#rejected",
        "partials/plan_progress.html#done",
        "partials/plan_progress.html#failed",
        "partials/plan_progress.html#gone",
    ),
    jobs=True,
)
def plan(ctx: FlowCtx) -> None:
    page = ctx.page
    llm = ctx.llm

    # ---- job 1: two rounds, then cancelled --------------------------------
    llm.control.canned(PLAN_V1)
    llm.hold()
    _start(ctx, "Sorting", "Sorting algorithms and the tradeoffs between them.")
    wait_status(ctx, "planning")
    wait_badge(ctx, "active workflows (1 of 1)")
    shot(ctx, "planning", after_swap=fresh_swap())

    llm.release()
    wait_status(ctx, "awaiting_feedback")
    wait_badge(ctx, "active workflows (1 of 1)")
    shot(ctx, "awaiting-round-1")

    llm.control.canned(PLAN_V2)
    llm.hold()
    page.fill(".plan-feedback-form textarea[name=feedback]", "Drop the mcq and add the worst case.")
    page.click(".plan-feedback-form button[type=submit]")
    wait_status(ctx, "replanning")
    shot(ctx, "replanning")

    llm.release()
    page.wait_for_selector('#t-status[data-status="awaiting_feedback"] .plan-round')
    shot(ctx, "awaiting-round-2")

    freeze_polling(ctx)
    page.click(".plan-decide form[action$='/reject'] button")
    wait_status(ctx, "rejecting")
    shot(ctx, "rejecting")
    resume_polling(ctx)

    wait_status(ctx, "rejected")
    wait_badge(ctx, "recently completed operations (1)")
    shot(ctx, "rejected")

    # ---- job 2: accepted, expanded, inserted ------------------------------
    llm.control.canned(PLAN_ONE)
    llm.hold()
    _start(ctx, "Lower bounds", "Why comparison sorting cannot beat n log n.")
    llm.release()
    wait_status(ctx, "awaiting_feedback")

    llm.control.canned(CARD)
    llm.hold()
    page.click(".plan-decide form[action$='/accept'] button")
    wait_status(ctx, "generating")
    wait_badge(ctx, "active workflows (1 of 2)")
    shot(ctx, "generating")

    llm.release()
    wait_status(ctx, "done")
    wait_badge(ctx, "recently completed operations (2)")
    shot(ctx, "done")

    # ---- job 3: every expansion comes back unparseable ---------------------
    # The failure has to be the workflow's own ("every card expansion
    # failed"), not an activity's: a Temporal activity error carries the
    # worker's `<pid>@<host>` identity into the rendered message, which is
    # neither stable across runs nor fit for a committed golden.
    llm.control.canned(PLAN_ONE)
    llm.hold()
    _start(ctx, "Broken", "A deck whose cards the model will not return as JSON.")
    llm.release()
    wait_status(ctx, "awaiting_feedback")

    llm.control.canned(NOT_JSON)
    page.click(".plan-decide form[action$='/accept'] button")
    wait_status(ctx, "failed")
    wait_badge(ctx, "recently completed operations (3)")
    shot(ctx, "failed")

    # ---- job 4: deleted, so the query has nothing to answer from ------------------
    llm.control.canned(PLAN_ONE)
    llm.hold()
    _start(ctx, "Abandoned", "A deck whose plan workflow is deleted mid-flight.")
    wait_status(ctx, "planning")
    wid = page.url.rstrip("/").rsplit("/", 1)[-1]
    ctx.jobs.abandon_workflow(wid)
    wait_status(ctx, "gone")
    wait_badge(ctx, "recently completed operations (4)")
    shot(ctx, "gone")

    llm.release()
    llm.control.canned(None)
