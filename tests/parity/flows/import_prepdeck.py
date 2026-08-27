from tests.parity.flows.io_files import prepdeck_body
from tests.parity.flows.io_steps import import_flow
from tests.parity.harness.registry import FlowCtx, flow


@flow("import-prepdeck", phase=5, seed="io", covers=("deck_import_prepdeck.html",))
def import_prepdeck(ctx: FlowCtx) -> None:
    import_flow(
        ctx,
        path="/decks/import-prepdeck",
        deck_name="graph-theory",
        filename="graph-theory.prepdeck",
        mime="application/zip",
        body=prepdeck_body(),
    )
