from tests.parity.harness.registry import FlowCtx, flow
from tests.parity.harness.steps import open_page

VERDICT = "h1.verdict-headline"


def _next(ctx: FlowCtx, then: str) -> None:
    ctx.page.get_by_role("button", name="Next card").click()
    ctx.page.wait_for_selector(then)


def _idk(ctx: FlowCtx) -> None:
    ctx.page.get_by_role("button", name="I don't know").click()
    ctx.page.wait_for_selector(VERDICT)


# Typed free-text answers book the LLM grader, so the short and code
# cards take the `idk` path; right and wrong come from the choice cards.
@flow("study", phase=3, seed="study", covers=("study_shell.html",))
def study(ctx: FlowCtx) -> None:
    page = ctx.page
    sid = ctx.seed["session_id"]
    badge = open_page(ctx, f"/session/{sid}", ".study-card label.choice")
    ctx.shot("mcq", after_swap=badge)

    page.locator("label.choice", has_text="Canberra").click()
    page.get_by_role("button", name="Submit").click()
    page.wait_for_selector(VERDICT)
    ctx.shot("mcq-right")

    _next(ctx, ".study-card textarea")
    ctx.shot("short")
    _idk(ctx)
    ctx.shot("short-idk")

    _next(ctx, ".study-card .choices-multi")
    ctx.shot("multi")
    page.locator("label.choice", has_text="Ottawa").click()
    page.get_by_role("button", name="Submit").click()
    page.wait_for_selector(VERDICT)
    ctx.shot("multi-wrong")

    _next(ctx, ".study-card .cm-editor")
    ctx.shot("code")
    _idk(ctx)

    _next(ctx, ".study-card textarea")
    ctx.shot("short-plain")
    _idk(ctx)

    _next(ctx, ".empty-headline")
    ctx.shot("done")
