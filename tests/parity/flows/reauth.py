from tests.parity.harness.registry import FlowCtx, flow


# The shell itself is served by `GET /` on a dormant Clerk session;
# the parity route renders it with the same context on any target.
# With the fallback cookie `GET /` serves the landing page instead.
@flow("reauth", phase=1, seed=None, covers=("reauth.html", "landing.html"), anonymous=True)
def reauth(ctx: FlowCtx) -> None:
    ctx.page.goto(ctx.url("/_parity/reauth"), wait_until="load")
    ctx.page.wait_for_selector(".reauth-shell")
    ctx.shot("shell")

    ctx.page.context.add_cookies(
        [{"name": "prep_reauth_fallback", "value": "1", "url": ctx.base_url}]
    )
    ctx.page.goto(ctx.url("/"), wait_until="load")
    ctx.page.wait_for_selector("main")
    ctx.shot("fallback-landing")
