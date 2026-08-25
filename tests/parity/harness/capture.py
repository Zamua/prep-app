"""Screenshot capture (docs/PARITY-GATE.md C3 and E)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from tests.e2e.flow_artifacts import _ANIMATIONS_SETTLED

ANIMATIONS_SETTLED = _ANIMATIONS_SETTLED
PERTURB_CSS_ENV = "PARITY_PERTURB_CSS"
PERTURB_CSS = "main{transform:translateY(1px)}"

_FONTS_READY = "() => document.fonts ? document.fonts.ready.then(() => true) : true"


@dataclass(frozen=True)
class SwapWaiter:
    """Resolves once `htmx:afterSwap` has fired more times than at
    creation. Create it BEFORE the action that starts polling."""

    baseline: int

    def wait(self, page, timeout_ms: int = 15_000) -> None:
        page.wait_for_function(
            "n => (window.__paritySwaps || 0) > n", arg=self.baseline, timeout=timeout_ms
        )


def expect_after_swap(page) -> SwapWaiter:
    try:
        baseline = int(page.evaluate("() => window.__paritySwaps || 0"))
    except Exception:  # noqa: BLE001  about:blank before the first navigation
        baseline = 0
    return SwapWaiter(baseline)


def settle(page, timeout_ms: int = 10_000) -> None:
    page.wait_for_function(_FONTS_READY, timeout=timeout_ms)
    page.wait_for_function(ANIMATIONS_SETTLED, timeout=timeout_ms)


def shot(page, path: Path, *, after_swap: SwapWaiter | None = None) -> Path:
    """Fonts ready, animations settled, the awaited swap landed, then
    a full-page device-scale screenshot with animations disabled and
    the caret hidden."""
    if after_swap is not None:
        after_swap.wait(page)
    settle(page)
    if os.environ.get(PERTURB_CSS_ENV) == "1":
        page.add_style_tag(content=PERTURB_CSS)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=str(path),
        full_page=True,
        animations="disabled",
        caret="hide",
        scale="device",
    )
    return path
