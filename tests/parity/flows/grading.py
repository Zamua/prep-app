"""The AI grade of a typed answer: waiting, then the verdict.

A free-text answer books the LLM judge, so holding the stub freezes the
study shell on its grading panel for as long as the capture needs.
"""

import json

from tests.parity.harness.registry import FlowCtx, flow
from tests.parity.harness.steps import open_page, shot, wait_badge, wait_badge_empty

ANSWER = "Theta(n log n) on average, because a random pivot splits the array evenly enough."

VERDICT = json.dumps(
    {
        "result": "right",
        "feedback": "Right, and you named the reason: a random pivot gives a balanced split in expectation, which is what makes the recursion depth logarithmic.",
        "model_answer_summary": "O(n log n) expected time.",
    }
)


@flow("grading", phase=4, seed="workflows", covers=("study_shell.html",), jobs=True)
def grading(ctx: FlowCtx) -> None:
    page = ctx.page
    llm = ctx.llm

    badge = open_page(ctx, f"/session/{ctx.seed['session_id']}", ".study-card textarea")
    page.fill(".study-card textarea", ANSWER)
    wait_badge_empty(ctx)
    shot(ctx, "answered", after_swap=badge)

    llm.control.canned(VERDICT)
    llm.hold()
    page.get_by_role("button", name="Submit").click()
    page.wait_for_selector(".grading-panel")
    wait_badge(ctx, "active workflows (1 of 1)")
    shot(ctx, "grading")

    llm.release()
    page.wait_for_selector("h1.verdict-headline")
    wait_badge(ctx, "recently completed operations (1)")
    shot(ctx, "verdict")

    llm.control.canned(None)
