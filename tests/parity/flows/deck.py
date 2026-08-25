from tests.parity.harness.registry import FlowCtx, flow


@flow("deck", phase=1, seed="reader", covers=("deck.html",))
def deck(ctx: FlowCtx) -> None:
    slug = ctx.seed["decks"]["srs_a"]["slug"]
    badge = ctx.expect_after_swap()
    ctx.page.goto(ctx.url(f"/deck/{slug}"), wait_until="load")
    ctx.page.wait_for_selector("[data-qid]")
    ctx.shot("populated", after_swap=badge)
