from tests.parity.flows.io_files import apkg_body
from tests.parity.flows.io_steps import import_flow
from tests.parity.harness.registry import FlowCtx, flow


@flow("import-anki", phase=5, seed="io", covers=("deck_import_anki.html",))
def import_anki(ctx: FlowCtx) -> None:
    import_flow(
        ctx,
        path="/decks/import-anki",
        deck_name="anki-import",
        filename="deck.apkg",
        mime="application/octet-stream",
        body=apkg_body(),
    )
