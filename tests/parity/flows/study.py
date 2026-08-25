from tests.parity.harness.registry import FlowCtx, flow


@flow("study", phase=3, seed="study", covers=("study_shell.html",))
def study(ctx: FlowCtx) -> None:
    sid = ctx.seed["session_id"]
    badge = ctx.expect_after_swap()
    ctx.page.goto(ctx.url(f"/session/{sid}"), wait_until="load")
    ctx.page.wait_for_selector(".study-card label.choice")
    ctx.shot("mcq", after_swap=badge)
