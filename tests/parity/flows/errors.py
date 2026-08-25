from tests.parity.harness.registry import FlowCtx, flow


# error.html carries no user, so no masthead polling to await.
@flow(
    "errors",
    phase=1,
    seed="empty",
    covers=("error.html", "error.html#404", "error.html#429", "error.html#500"),
)
def errors(ctx: FlowCtx) -> None:
    for label, path, status in (
        ("404", "/no-such-page-parity", 404),
        ("429", "/_parity/raise?status=429", 429),
        ("500", "/_parity/raise", 500),
    ):
        response = ctx.page.goto(ctx.url(path), wait_until="load")
        assert response is not None and response.status == status, response and response.status
        ctx.page.wait_for_selector(".error-block")
        ctx.shot(label)
