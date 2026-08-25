from tests.parity.harness.registry import FlowCtx, flow
from tests.parity.harness.steps import open_page

# A key of the accepted shape; only its mask ever renders.
BYOK_KEY = "sk-ant-api03-parity0000000000000000000000000000000000000000000000000000000000000000000000ParityAA"


@flow(
    "settings",
    phase=1,
    seed="reader",
    covers=(
        "settings_agent.html",
        "settings_srs.html",
        "settings_editor.html",
        "settings_api.html",
        "notify_settings.html",
        "notify/log.html",
    ),
)
def settings(ctx: FlowCtx) -> None:
    page = ctx.page

    badge = open_page(ctx, "/settings/agent", "main .prelude")
    ctx.shot("agent-none", after_swap=badge)

    # Same-origin fetch so the identity headers ride along.
    status = page.evaluate(
        """async (key) => {
          const r = await fetch("/settings/agent/byok/anthropic-api/connect", {
            method: "POST",
            body: new URLSearchParams({api_key: key}),
          });
          return r.status;
        }""",
        BYOK_KEY,
    )
    assert status == 200, status
    badge = open_page(ctx, "/settings/agent", "main .prelude")
    ctx.shot("agent-connected", after_swap=badge)

    for label, path in (
        ("srs", "/settings/srs"),
        ("editor", "/settings/editor"),
        ("api", "/settings/api"),
        ("notify", "/notify"),
        ("notify-log", "/notify/log"),
    ):
        badge = open_page(ctx, path, "main .prelude")
        ctx.shot(label, after_swap=badge)
