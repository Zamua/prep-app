from tests.parity.harness.registry import FlowCtx, flow
from tests.parity.harness.steps import open_page


@flow("dashboard-empty", phase=1, seed="empty", covers=("index.html",))
def dashboard_empty(ctx: FlowCtx) -> None:
    badge = open_page(ctx, "/", "[data-dashboard-decks] .empty-state")
    ctx.shot("empty", after_swap=badge)
