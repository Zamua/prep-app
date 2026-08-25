"""Browser context for a parity capture (docs/PARITY-GATE.md C2)."""

from __future__ import annotations

from urllib.parse import urlparse

from tests.parity.harness.constants import (
    PARITY_NOW,
    PARITY_TZ,
    PARITY_USER,
    PARITY_USER_NAME,
)

IOS_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4 Mobile/15E148 Safari/604.1"
)
VIEWPORT = {"width": 393, "height": 852}
DEVICE_SCALE_FACTOR = 3

# htmx events bubble to `document`; counting them from document start
# means a swap that lands before the flow looks is still seen.
_SWAP_COUNTER = """
window.__paritySwaps = 0;
document.addEventListener('htmx:afterSwap', () => { window.__paritySwaps += 1; });
"""


def new_context(
    browser,
    scheme: str,
    *,
    base_url: str,
    service_workers: str = "block",
    identity: str | None = PARITY_USER,
    storage_state=None,
    default_timeout_ms: int = 20_000,
):
    """393x852 at DPR 3, iOS UA, touch, reduced motion, the parity tz
    and locale, `color_scheme` per `scheme`. Same-origin requests carry
    the tailscale identity headers for `identity`; `None` leaves the
    context anonymous. A Clerk target passes `storage_state` instead."""
    if scheme not in ("light", "dark"):
        raise ValueError(f"scheme must be light|dark, got {scheme!r}")
    if service_workers not in ("block", "allow"):
        raise ValueError(f"service_workers must be block|allow, got {service_workers!r}")
    ctx = browser.new_context(
        user_agent=IOS_UA,
        viewport=VIEWPORT,
        device_scale_factor=DEVICE_SCALE_FACTOR,
        is_mobile=True,
        has_touch=True,
        reduced_motion="reduce",
        color_scheme=scheme,
        timezone_id=PARITY_TZ,
        locale="en-US",
        service_workers=service_workers,
        storage_state=storage_state,
    )
    ctx.set_default_timeout(default_timeout_ms)
    ctx.set_default_navigation_timeout(default_timeout_ms)
    ctx.add_init_script(_SWAP_COUNTER)

    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    if identity:

        def _inject(route, request):
            if request.url.startswith(origin):
                route.continue_(
                    headers={
                        **request.headers,
                        "tailscale-user-login": identity,
                        "tailscale-user-name": PARITY_USER_NAME,
                    }
                )
            else:
                route.continue_()

        ctx.route("**/*", _inject)
    return ctx


def new_page(ctx):
    """A page whose `Date` is pinned to `PARITY_NOW`; timers keep
    running so htmx polling does."""
    page = ctx.new_page()
    page.clock.set_fixed_time(PARITY_NOW)
    return page
