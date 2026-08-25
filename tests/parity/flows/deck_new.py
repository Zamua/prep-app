from tests.parity.harness.registry import FlowCtx, flow
from tests.parity.harness.steps import open_page


@flow(
    "deck-new",
    phase=1,
    seed="reader",
    covers=("deck_new_chooser.html", "deck_new_srs.html", "deck_new_trivia.html"),
)
def deck_new(ctx: FlowCtx) -> None:
    for label, path in (
        ("chooser", "/decks/new"),
        ("srs", "/decks/new/srs"),
        ("trivia", "/decks/new/trivia"),
    ):
        badge = open_page(ctx, path, "main .prelude, main form")
        ctx.shot(label, after_swap=badge)
