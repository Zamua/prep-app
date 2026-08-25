from tests.parity.harness.registry import FlowCtx, flow


@flow("landing", phase=1, seed="empty", covers=("landing.html",), anonymous=True)
def landing(ctx: FlowCtx) -> None:
    ctx.page.goto(ctx.url("/"), wait_until="load")
    ctx.page.wait_for_selector("main")
    ctx.shot("splash")
