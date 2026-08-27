"""Deck- and card-scope transforms, every status and every diff-card field.

The plan the worker returns is the stub's canned answer, so the nine
fields `transform_diff_card.html` can show, its no-change branch, and
the addition-list overflow are all one authored payload against the
seeded card ids.

No `gone` shot: the plan flow already holds it, and the two partials
render the same branch. `awaiting_apply` is one shot, not three: the
capture is full-page, so a single screenshot carries every diff card.

No `failed` shot either. Every Transform failure path renders the step's
own error, which carries a per-run identity, so the text differs on every
run. The plan and trivia flows cover the failed branch through their
workflows' fixed messages instead.
"""

import json

from tests.parity.harness.registry import FlowCtx, flow
from tests.parity.harness.steps import (
    click_and_wait,
    freeze_polling,
    fresh_swap,
    open_page,
    resume_polling,
    shot,
    wait_badge,
    wait_badge_empty,
    wait_status,
)


def _deck_plan(qids: dict) -> str:
    """One plan touching every field the diff card can render, plus a
    modification that changes nothing and an addition list that
    overflows its three-item preview."""
    return json.dumps(
        {
            "notes": "Tightened four cards, added five on lower bounds, dropped a duplicate.",
            "modifications": [
                {
                    "question_id": qids["complexity"],
                    "type": "short",
                    "topic": "asymptotics",
                    "prompt": "What is the expected running time of quicksort on a random permutation?",
                    "answer": "Theta(n log n) expected, over the randomness of the pivot choices.",
                },
                {
                    "question_id": qids["traversal"],
                    "type": "multi",
                    "topic": "graphs",
                    "prompt": "Which traversal visits a graph level by level?",
                    "answer": "Breadth-first search",
                },
                {
                    "question_id": qids["binary_search"],
                    "type": "code",
                    "topic": "searching",
                    "prompt": "Return the index of `target` in the sorted list `xs`, or -1.",
                    "answer": "def find(xs, target):\n    lo, hi = 0, len(xs) - 1\n    while lo <= hi:\n        mid = (lo + hi) // 2\n        if xs[mid] == target:\n            return mid\n        if xs[mid] < target:\n            lo = mid + 1\n        else:\n            hi = mid - 1\n    return -1\n",
                    "rubric": "- Halves the range each step\n- Returns -1 on a miss\n- Avoids overflow in the midpoint",
                    "skeleton": "def find(xs, target):\n    lo, hi = 0, len(xs) - 1\n    ...\n",
                    "language": "go",
                },
                {
                    "question_id": qids["annotated"],
                    "type": "short",
                    "topic": "sorting",
                    "prompt": "Which sort is stable: heapsort or merge sort?",
                    "answer": "Merge sort",
                    "explanation": "Merge sort compares across runs and keeps the left element on ties, so equal keys stay in input order. Heapsort moves elements through a heap and loses that order.",
                    "answer_regex": "(?i)^\\s*merge(\\s+sort)?\\s*$",
                },
                {
                    "question_id": qids["retired"],
                    "type": "short",
                    "topic": "history",
                    "prompt": "Which sort did the 1959 Shell paper describe?",
                    "answer": "Shellsort",
                },
            ],
            "additions": [
                {
                    "type": "short",
                    "topic": "lower-bounds",
                    "prompt": "Why does the comparison model bound sorting below by n log n?",
                    "answer": "A decision tree over n! permutations has height at least log2(n!).",
                },
                {
                    "type": "short",
                    "topic": "lower-bounds",
                    "prompt": "What lets radix sort beat the comparison lower bound?",
                    "answer": "It reads the keys' digits instead of comparing whole keys.",
                },
                {
                    "type": "mcq",
                    "topic": "lower-bounds",
                    "prompt": "The height of a binary decision tree with n! leaves is at least:",
                    "choices": ["log2(n)", "n", "log2(n!)", "n!"],
                    "answer": "log2(n!)",
                },
                {
                    "type": "short",
                    "topic": "selection",
                    "prompt": "What is the worst-case cost of median-of-medians selection?",
                    "answer": "Linear in the number of elements.",
                },
                {
                    "type": "short",
                    "topic": "sorting",
                    "prompt": "When does insertion sort beat merge sort in practice?",
                    "answer": "On short or nearly sorted inputs, where its constant factor wins.",
                },
            ],
            "deletions": [qids["duplicate"]],
        }
    )


def _additions_plan(qids: dict) -> str:
    """A plan whose whole body comes from the plan payload, not the rows."""
    return json.dumps(
        {
            "notes": "Added two cards on isolation levels and dropped the WAL card.",
            "additions": [
                {
                    "type": "short",
                    "topic": "isolation",
                    "prompt": "Which isolation level still permits a write skew?",
                    "answer": "Snapshot isolation.",
                },
                {
                    "type": "mcq",
                    "topic": "isolation",
                    "prompt": "Which anomaly does read-committed still allow?",
                    "choices": ["Dirty read", "Non-repeatable read", "Neither"],
                    "answer": "Non-repeatable read",
                },
            ],
            "deletions": [qids["wal"]],
        }
    )


def _card_plan(qid: int) -> str:
    return json.dumps(
        {
            "notes": "Named the guarantee the card is really asking about.",
            "modifications": [
                {
                    "question_id": qid,
                    "type": "short",
                    "topic": "transactions",
                    "prompt": "What does the I in ACID guarantee about concurrent transactions?",
                    "answer": "No transaction observes another's partial writes; the schedule is equivalent to some serial order.",
                }
            ],
        }
    )


def _ask(ctx: FlowCtx, path: str, prompt: str) -> None:
    """Fill an AI prompt form and wait out the redirect to the job page."""
    page = ctx.page
    page.goto(ctx.url(path), wait_until="load")
    page.fill("form.transform-form textarea[name=prompt]", prompt)
    with page.expect_navigation(wait_until="load"):
        page.click("form.transform-form button[type=submit]")
    page.wait_for_selector("#transform-progress")


@flow(
    "transform",
    phase=4,
    seed="workflows",
    covers=(
        "transform.html",
        "deck_edit_ai.html",
        "partials/transform_progress.html#computing",
        "partials/transform_progress.html#awaiting_apply",
        "partials/transform_progress.html#applying",
        "partials/transform_progress.html#rejecting",
        "partials/transform_progress.html#rejected",
        "partials/transform_progress.html#done",
        "partials/transform_diff_card.html",
    ),
    jobs=True,
)
def transform(ctx: FlowCtx) -> None:
    page = ctx.page
    llm = ctx.llm
    decks = ctx.seed["decks"]
    a_ids = ctx.seed["questions"]["srs_a"]
    b_ids = ctx.seed["questions"]["srs_b"]

    # ---- deck scope: computing, review, apply -----------------------------
    badge = open_page(ctx, f"/deck/{decks['srs_a']['slug']}/edit-with-ai", "form.transform-form")
    wait_badge_empty(ctx)
    shot(ctx, "edit-with-ai", after_swap=badge)

    llm.control.canned(_deck_plan(a_ids))
    llm.hold()
    _ask(
        ctx,
        f"/deck/{decks['srs_a']['slug']}/edit-with-ai",
        "Tighten the wording, add cards on lower bounds, drop the duplicate.",
    )
    wait_status(ctx, "computing")
    wait_badge(ctx, "active workflows (1 of 1)")
    shot(ctx, "computing", after_swap=fresh_swap())

    llm.release()
    wait_status(ctx, "awaiting_apply")
    wait_badge(ctx, "active workflows (1 of 1)")
    shot(ctx, "awaiting-apply")

    page.click(".transform-actions form[action$='/apply'] button")
    wait_status(ctx, "done")
    wait_badge(ctx, "recently completed operations (1)")
    shot(ctx, "done")

    # ---- deck scope: applying --------------------------------------------
    # Its own job, and a plan with no modifications. A modification's diff
    # is built from the live rows, and the apply activity is rewriting
    # exactly those rows while the post-signal fragment renders, so a
    # `applying` shot of that plan is a coin flip between the before and
    # after shapes. Additions and deletions render from the plan alone.
    llm.control.canned(_additions_plan(b_ids))
    llm.hold()
    _ask(ctx, f"/deck/{decks['srs_b']['slug']}/edit-with-ai", "Add two cards on isolation levels.")
    llm.release()
    wait_status(ctx, "awaiting_apply")
    wait_badge(ctx, "active workflows (1 of 2)")

    freeze_polling(ctx)
    page.click(".transform-actions form[action$='/apply'] button")
    wait_status(ctx, "applying")
    shot(ctx, "applying")
    resume_polling(ctx)
    wait_status(ctx, "done")

    # ---- deck scope: rejected --------------------------------------------
    llm.control.canned(_card_plan(b_ids["acid"]))
    llm.hold()
    _ask(ctx, f"/deck/{decks['srs_b']['slug']}/edit-with-ai", "Sharpen the ACID card.")
    llm.release()
    wait_status(ctx, "awaiting_apply")
    # The badge has to be settled BEFORE the signal: `rejecting` lasts
    # only until the next 2s poll, which is no room for a 5s badge poll.
    wait_badge(ctx, "active workflows (1 of 3)")

    freeze_polling(ctx)
    page.click(".transform-actions form[action$='/reject'] button")
    wait_status(ctx, "rejecting")
    shot(ctx, "rejecting")
    resume_polling(ctx)

    wait_status(ctx, "rejected")
    wait_badge(ctx, "recently completed operations (3)")
    shot(ctx, "rejected")

    # ---- card scope: the improve dialog, then an auto-apply ---------------
    badge = open_page(ctx, f"/deck/{decks['srs_b']['slug']}", "[data-qid]")
    click_and_wait(
        ctx,
        f'.qcard-action--improve[data-qid="{b_ids["acid"]}"]',
        "#improve-dialog[open]",
    )
    wait_badge(ctx, "recently completed operations (3)")
    shot(ctx, "improve-dialog")

    llm.control.canned(_card_plan(b_ids["acid"]))
    llm.hold()
    page.fill("#improve-prompt", "Say what the guarantee actually is.")
    with page.expect_navigation(wait_until="load"):
        page.click("#improve-form button[type=submit]")
    page.wait_for_selector("#transform-progress")
    wait_status(ctx, "computing")
    wait_badge(ctx, "active workflows (1 of 4)")
    shot(ctx, "card-computing", after_swap=fresh_swap())

    llm.release()
    wait_status(ctx, "done")
    wait_badge(ctx, "recently completed operations (4)")
    shot(ctx, "card-done")

    llm.control.canned(None)
