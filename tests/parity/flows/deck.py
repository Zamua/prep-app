from tests.parity.harness.registry import FlowCtx, flow
from tests.parity.harness.steps import click_and_wait, fresh_swap, open_page


@flow(
    "deck",
    phase=1,
    seed="reader",
    covers=("deck.html", "partials/deck_overflow_menu.html"),
)
def deck(ctx: FlowCtx) -> None:
    page = ctx.page
    decks = ctx.seed["decks"]

    badge = open_page(ctx, f"/deck/{decks['srs_a']['slug']}", "[data-qid]")
    ctx.shot("populated", after_swap=badge)

    click_and_wait(ctx, ".deck-overflow-menu > summary", ".deck-overflow-menu[open]")
    ctx.shot("overflow")

    click_and_wait(ctx, "#open-delete-dialog", "#delete-deck-dialog[open]")
    ctx.shot("delete-dialog")
    page.click("#cancel-delete")
    page.wait_for_selector("#delete-deck-dialog[open]", state="hidden")

    # The pin row is a plain form: POST, 303, the deck page again.
    if not page.locator(".deck-overflow-menu[open]").count():
        click_and_wait(ctx, ".deck-overflow-menu > summary", ".deck-overflow-menu[open]")
    with page.expect_navigation(wait_until="load"):
        page.click(".deck-overflow-menu .deck-pin-menu-form button")
    page.wait_for_selector("[data-qid]")
    ctx.shot("pinned", after_swap=fresh_swap())

    badge = open_page(ctx, f"/deck/{decks['trivia']['slug']}", "[data-qid]")
    ctx.shot("trivia", after_swap=badge)

    badge = open_page(ctx, f"/deck/{decks['empty']['slug']}", ".empty-state")
    ctx.shot("empty", after_swap=badge)
