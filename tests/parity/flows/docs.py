from tests.parity.harness.registry import FlowCtx, flow


# Vendor shells stripped of their cross-origin bundles under the flag.
@flow("docs", phase=1, seed=None, covers=("@swagger", "@redoc"), anonymous=True)
def docs(ctx: FlowCtx) -> None:
    for label, path in (("swagger", "/docs"), ("redoc", "/redoc")):
        ctx.page.goto(ctx.url(path), wait_until="load")
        ctx.page.wait_for_selector("body", state="attached")
        ctx.shot(label)
