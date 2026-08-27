"""The cross-deck reorganize scope: its form and its per-deck plan.

Reorganize is the only scope that renders `new_decks`, `deck_renames`,
`card_moves` and `deck_deletions`, and the only one that buckets
modifications, additions and deletions by deck. One canned plan carries
all of it against the seeded ids.
"""

import json

from tests.parity.harness.registry import FlowCtx, flow
from tests.parity.harness.steps import (
    fresh_swap,
    open_page,
    shot,
    wait_badge,
    wait_badge_empty,
    wait_status,
)

NEW_DECK = "lower-bounds"


def _plan(decks: dict, a_ids: dict, b_ids: dict) -> str:
    return json.dumps(
        {
            "notes": "Split the lower-bound material out and folded the trivia deck away.",
            "new_decks": [
                {
                    "name": NEW_DECK,
                    "deck_type": "srs",
                    "topic": "Lower bounds on sorting, searching and selection.",
                }
            ],
            "deck_renames": [{"deck_id": decks["srs_b"]["id"], "new_name": "storage-engines"}],
            "card_moves": [
                {"question_id": a_ids["complexity"], "dest_deck": NEW_DECK},
                {"question_id": a_ids["annotated"], "dest_deck": NEW_DECK},
                {"question_id": b_ids["wal"], "dest_deck": decks["srs_a"]["slug"]},
            ],
            "modifications": [
                {
                    "question_id": a_ids["traversal"],
                    "type": "mcq",
                    "topic": "graphs",
                    "prompt": "Which traversal visits a graph one level at a time?",
                    "answer": "Breadth-first search",
                },
                {
                    "question_id": b_ids["btree"],
                    "type": "mcq",
                    "topic": "indexes",
                    "prompt": "Which index shape keeps range scans sequential on disk?",
                    "answer": "B-tree",
                    "explanation": "A B-tree keeps keys ordered in its leaves, so a range scan walks neighbours.",
                },
            ],
            "additions": [
                {
                    "dest_deck": NEW_DECK,
                    "type": "short",
                    "topic": "lower-bounds",
                    "prompt": "Why does the comparison model bound sorting below by n log n?",
                    "answer": "A decision tree over n! permutations has height at least log2(n!).",
                },
                {
                    "dest_deck": decks["srs_a"]["slug"],
                    "type": "short",
                    "topic": "selection",
                    "prompt": "What is the worst-case cost of median-of-medians selection?",
                    "answer": "Linear in the number of elements.",
                },
            ],
            "deletions": [a_ids["duplicate"], b_ids["acid"]],
            "deck_deletions": [decks["trivia"]["id"]],
        }
    )


@flow(
    "reorganize",
    phase=4,
    seed="workflows",
    covers=(
        "reorganize.html",
        "partials/transform_progress.html#reorganize",
        "partials/transform_progress.html#new_decks",
        "partials/transform_progress.html#deck_renames",
        "partials/transform_progress.html#card_moves",
        "partials/transform_progress.html#deck_deletions",
    ),
    jobs=True,
)
def reorganize(ctx: FlowCtx) -> None:
    page = ctx.page
    llm = ctx.llm

    badge = open_page(ctx, "/reorganize", "form.transform-form")
    wait_badge_empty(ctx)
    shot(ctx, "form", after_swap=badge)

    questions = ctx.seed["questions"]
    llm.control.canned(_plan(ctx.seed["decks"], questions["srs_a"], questions["srs_b"]))
    llm.hold()
    page.fill(
        "form.transform-form textarea[name=prompt]",
        "Pull the lower-bound cards into their own deck and retire the trivia deck.",
    )
    with page.expect_navigation(wait_until="load"):
        page.click("form.transform-form button[type=submit]")
    page.wait_for_selector("#transform-progress")
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

    llm.control.canned(None)
