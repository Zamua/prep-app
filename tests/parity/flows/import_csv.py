from tests.parity.flows.io_files import csv_body
from tests.parity.flows.io_steps import import_flow
from tests.parity.harness.registry import FlowCtx, flow


@flow("import-csv", phase=5, seed="io", covers=("deck_import_csv.html",))
def import_csv(ctx: FlowCtx) -> None:
    import_flow(
        ctx,
        path="/decks/import-csv",
        deck_name="graph-csv",
        filename="graphs.csv",
        mime="text/csv",
        body=csv_body(),
    )
