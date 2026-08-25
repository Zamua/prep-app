from tests.parity.harness.registry import FlowCtx, flow
from tests.parity.harness.steps import open_page


@flow("question", phase=1, seed="reader", covers=("question_new.html", "question_edit.html"))
def question(ctx: FlowCtx) -> None:
    slug = ctx.seed["decks"]["srs_a"]["slug"]
    code_qid = ctx.seed["questions"]["srs_a"]["code"]

    badge = open_page(ctx, f"/deck/{slug}/question/new", "form textarea[name=prompt]")
    ctx.shot("new", after_swap=badge)

    badge = open_page(ctx, f"/question/{code_qid}/edit", "form textarea[name=prompt]")
    ctx.shot("edit", after_swap=badge)
