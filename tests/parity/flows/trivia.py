from tests.parity.harness.registry import FlowCtx, flow
from tests.parity.harness.steps import fresh_swap, open_page

ANSWERS = {"476": "The Roman Empire", "printing": "Gutenberg", "Magna Carta": "1215"}
FORM = ".trivia-answer-form"
RESULT = ".trivia-result"


def _answer_for(ctx: FlowCtx) -> str:
    prompt = ctx.page.locator(".trivia-prompt").inner_text()
    for needle, answer in ANSWERS.items():
        if needle in prompt:
            return answer
    raise AssertionError(f"unexpected trivia prompt: {prompt!r}")


def _submit_right(ctx: FlowCtx) -> None:
    page = ctx.page
    page.fill(f"{FORM} input[name=answer]", _answer_for(ctx))
    with page.expect_navigation(wait_until="load"):
        page.click("#trivia-submit-btn")
    page.wait_for_selector(RESULT)


def _next(ctx: FlowCtx, then: str) -> None:
    with ctx.page.expect_navigation(wait_until="load"):
        ctx.page.click(".trivia-next-cta")
    ctx.page.wait_for_selector(then)


@flow(
    "trivia",
    phase=3,
    seed="reader",
    covers=("trivia/card.html", "trivia/session_done.html"),
)
def trivia(ctx: FlowCtx) -> None:
    page = ctx.page
    slug = ctx.seed["decks"]["trivia"]["slug"]
    deep_link = ctx.seed["questions"]["trivia"]["print"]

    badge = open_page(ctx, f"/trivia/{deep_link}", FORM)
    ctx.shot("deep-link", after_swap=badge)

    # A fresh session draws its queue at random; an explicit queue is
    # the same page with the order fixed.
    q = ctx.seed["questions"]["trivia"]
    queue = ",".join(str(q[k]) for k in ("print", "magna", "rome"))
    badge = open_page(ctx, f"/trivia/session/{slug}?cards={queue}", FORM)
    ctx.shot("session-card", after_swap=badge)

    # Every trivia card carries a regex, so grading is deterministic.
    _submit_right(ctx)
    ctx.shot("session-right", after_swap=fresh_swap())

    _next(ctx, FORM)
    with page.expect_navigation(wait_until="load"):
        page.click(f"{FORM} button[name=idk]")
    page.wait_for_selector(RESULT)
    ctx.shot("session-idk", after_swap=fresh_swap())

    while page.locator(".trivia-next-cta").inner_text().strip() == "Next card":
        _next(ctx, FORM)
        _submit_right(ctx)

    _next(ctx, ".ts-summary-head")
    ctx.shot("done", after_swap=fresh_swap())
