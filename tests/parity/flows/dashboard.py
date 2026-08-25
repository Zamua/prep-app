from tests.parity.harness.registry import FlowCtx, flow
from tests.parity.harness.steps import click_and_wait, open_page


@flow(
    "dashboard",
    phase=1,
    seed="reader",
    covers=(
        "index.html",
        "partials/deck_menus.html",
        "partials/deck_overflow_menu.html",
        "partials/sheet_duration.html",
        "partials/workflow_badge.html",
        "partials/pwa_install_nudge.html",
    ),
)
def dashboard(ctx: FlowCtx) -> None:
    page = ctx.page
    badge = open_page(ctx, "/", "[data-dashboard-decks] .deck-card")
    ctx.shot("populated", after_swap=badge)

    page.click("details[data-deck-menu] > summary")
    page.wait_for_selector("details[data-deck-menu][open]")
    ctx.shot("deck-menu")
    page.click("details[data-deck-menu][open] > summary")

    page.click("details.session-menu > summary")
    page.wait_for_selector("details.session-menu[open]")
    ctx.shot("session-menu")

    click_and_wait(ctx, "details.session-menu [data-sheet-open]", "#duration-sheet[open]")
    ctx.shot("duration-sheet")
    page.click("#duration-sheet [data-sheet-cancel]")
    page.wait_for_selector("#duration-sheet[open]", state="hidden")

    click_and_wait(ctx, ".pwa-install-pill", ".pwa-install-dialog[open]")
    ctx.shot("install-nudge")
