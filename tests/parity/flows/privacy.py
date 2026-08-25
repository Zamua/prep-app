from tests.parity.harness.registry import FlowCtx, flow


# The page renders without a user, so no masthead polling to await.
@flow("privacy", phase=1, seed="empty", covers=("privacy.html",), anonymous=True)
def privacy(ctx: FlowCtx) -> None:
    ctx.page.goto(ctx.url("/privacy"), wait_until="load")
    ctx.page.wait_for_selector("main")
    ctx.shot("privacy")
