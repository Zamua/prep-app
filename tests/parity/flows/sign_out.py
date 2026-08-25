from tests.parity.harness.registry import FlowCtx, flow
from tests.parity.harness.steps import open_page


# The interstitial is what `GET /sign-out` renders under Clerk; the
# parity route renders it on any target. The device-wipe choice is
# built by the sign-out guard from `offline/wipe.js`, opened here
# directly since only a provider with a sign-out URL renders the row.
@flow("sign-out", phase=1, seed="reader", covers=("sign_out_interstitial.html", "index.html"))
def sign_out(ctx: FlowCtx) -> None:
    page = ctx.page
    page.goto(ctx.url("/_parity/sign-out"), wait_until="load")
    page.wait_for_selector("main .prelude")
    ctx.shot("interstitial")

    badge = open_page(ctx, "/", "[data-dashboard-decks] .deck-card")
    badge.wait(page)
    page.evaluate("() => import('@/offline/wipe.js').then((m) => { m.confirmSignOut(); })")
    page.wait_for_selector("dialog.offline-signout-dialog[open]")
    ctx.shot("device-wipe-dialog")
