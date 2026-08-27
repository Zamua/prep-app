"""The trivia batch-generation page, held and landed.

No `applying` shot. The insert step is one activity per pair with no
LLM call in it, so the whole run between `generating` and `done` is a
few milliseconds against a 1.5s poll: there is nothing to hold it open
with and no window a capture could land in.
"""

import json

from tests.parity.harness.registry import FlowCtx, flow
from tests.parity.harness.steps import fresh_swap, shot, wait_badge, wait_status

PAIRS = json.dumps(
    [
        {
            "q": "Which scheduling policy can starve a long job indefinitely?",
            "a": "Shortest job first",
            "e": "A stream of shorter arrivals keeps preempting the long one.",
        },
        {
            "q": "What does the TLB cache?",
            "a": "Virtual to physical page translations",
            "e": "It is a small associative cache in front of the page tables.",
        },
        {
            "q": "Which memory-ordering model lets a store be reordered after a later load?",
            "a": "Total store order",
            "e": "TSO keeps a store buffer, so a load may pass a pending store.",
        },
        {
            "q": "What does a page fault cost that a cache miss does not?",
            "a": "A trap into the kernel",
            "e": "The fault handler runs before the instruction can be retried.",
        },
        {
            "q": "Why does DMA need cache coherence handling?",
            "a": "The device writes memory the CPU may hold stale in cache",
            "e": "Drivers either invalidate the range or map it uncached.",
        },
    ]
)

# An empty batch fails through the workflow's own fixed message. A parse
# failure would render the step's own error instead, which carries a
# per-run identity and cannot be a golden.
EMPTY_BATCH = "[]"


def _new_trivia_deck(ctx: FlowCtx, name: str, topic: str) -> None:
    page = ctx.page
    page.goto(ctx.url("/decks/new/trivia"), wait_until="load")
    page.fill("input[name=name]", name)
    page.fill("textarea[name=topic]", topic)
    with page.expect_navigation(wait_until="load"):
        page.click("form button[type=submit]")
    page.wait_for_selector("#trivia-gen-progress")


@flow(
    "trivia-generating",
    phase=4,
    seed="workflows",
    covers=(
        "trivia/generating.html",
        "partials/trivia_generating_progress.html#generating",
        "partials/trivia_generating_progress.html#done",
        "partials/trivia_generating_progress.html#failed",
    ),
    jobs=True,
)
def trivia_generating(ctx: FlowCtx) -> None:
    llm = ctx.llm

    llm.control.canned(PAIRS)
    llm.hold()
    _new_trivia_deck(ctx, "Kernel Trivia", "Operating system internals and CPU architecture.")
    wait_status(ctx, "generating")
    wait_badge(ctx, "active workflows (1 of 1)")
    shot(ctx, "generating", after_swap=fresh_swap())

    llm.release()
    wait_status(ctx, "done")
    wait_badge(ctx, "recently completed operations (1)")
    shot(ctx, "done")

    llm.control.canned(EMPTY_BATCH)
    llm.hold()
    _new_trivia_deck(ctx, "Broken Trivia", "A topic the model finds nothing to ask about.")
    wait_status(ctx, "generating")
    llm.release()
    wait_status(ctx, "failed")
    wait_badge(ctx, "recently completed operations (2)")
    shot(ctx, "failed")

    llm.control.canned(None)
