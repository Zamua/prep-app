from tests.parity.harness.registry import FlowCtx, flow


# error.html carries no user, so no masthead polling to await.
@flow("errors", phase=1, seed="empty", covers=("error.html", "error.html#404"))
def errors(ctx: FlowCtx) -> None:
    response = ctx.page.goto(ctx.url("/no-such-page-parity"), wait_until="load")
    assert response is not None and response.status == 404, response and response.status
    ctx.page.wait_for_selector("main")
    ctx.shot("404")
