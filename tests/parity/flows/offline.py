from tests.parity.harness.registry import FlowCtx, flow


@flow("offline", phase=3, seed="reader", covers=("offline.html",), service_workers="allow")
def offline(ctx: FlowCtx) -> None:
    ctx.page.goto(ctx.url("/offline"), wait_until="load")
    ctx.page.wait_for_selector("[data-offline-root] .prelude")
    ctx.shot("shell")
