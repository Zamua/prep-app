from tests.parity.harness.registry import FlowCtx, flow


@flow(
    "dashboard",
    phase=1,
    seed="reader",
    covers=(
        "index.html",
        "partials/deck_menus.html",
        "partials/workflow_badge.html",
        "partials/pwa_install_nudge.html",
    ),
)
def dashboard(ctx: FlowCtx) -> None:
    badge = ctx.expect_after_swap()
    ctx.page.goto(ctx.url("/"), wait_until="load")
    ctx.page.wait_for_selector("[data-dashboard-decks] .deck-card")
    ctx.shot("populated", after_swap=badge)
