from tests.parity.harness.registry import FlowCtx, flow
from tests.parity.harness.steps import open_page, wait_for_snapshot


# The shell renders from IndexedDB, so it is captured empty first, then
# after one online dashboard load has written the snapshot. Service
# workers stay blocked: a controlling worker re-issues the snapshot
# fetch without the injected identity header, which trips the owner
# guard and wipes the stores mid-flow.
@flow("offline", phase=3, seed="reader", covers=("offline.html",))
def offline(ctx: FlowCtx) -> None:
    page = ctx.page
    page.goto(ctx.url("/offline"), wait_until="load")
    page.wait_for_selector("[data-offline-root] .prelude")
    ctx.shot("shell")

    open_page(ctx, "/", "[data-dashboard-decks] .deck-card")
    wait_for_snapshot(ctx, cards=1)

    page.goto(ctx.url("/offline"), wait_until="load")
    page.wait_for_selector("[data-offline-root] .due-strip")
    ctx.shot("dashboard")

    page.get_by_role("button", name="Study").click()
    page.wait_for_selector("[data-offline-root] .study-card")
    ctx.shot("study")
