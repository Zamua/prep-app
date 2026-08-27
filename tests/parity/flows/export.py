from tests.parity.harness.registry import FlowCtx, flow
from tests.parity.harness.steps import open_page, shot


@flow("export", phase=5, seed="io", covers=("deck_export.html",))
def export(ctx: FlowCtx) -> None:
    """The hub for both deck types. Only the download buttons act, and a
    download is not a page, so what the gate holds is the hub itself."""
    for label, slug in (("srs", "algorithms"), ("trivia", "database-trivia")):
        badge = open_page(ctx, f"/deck/{slug}/export", "main .export-options")
        shot(ctx, label, after_swap=badge)
