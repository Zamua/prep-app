from tests.parity.harness.registry import FlowCtx, flow
from tests.parity.harness.steps import open_page, shot, submit_form

SUBMIT = "form.deck-split-form button[type=submit]"


@flow("split", phase=5, seed="io", covers=("deck_split.html",))
def split(ctx: FlowCtx) -> None:
    badge = open_page(ctx, "/deck/algorithms/split", "form.deck-split-form")
    shot(ctx, "form", after_swap=badge)

    # A name and no cards: the checkboxes carry no `required`, so this is a
    # post a browser makes and the service refuses.
    ctx.page.fill('input[name="new_name"]', "searching")
    submit_form(ctx, SUBMIT, "main .error-banner")
    shot(ctx, "error")

    ctx.page.fill('input[name="new_name"]', "searching")
    # Forced: the checkbox itself is under its styled marker, which is what
    # a tap hits and what Playwright's actionability check refuses.
    boxes = ctx.page.locator('input[name="question_ids"]')
    for i in range(2):
        boxes.nth(i).check(force=True)
    shot(ctx, "selected")
