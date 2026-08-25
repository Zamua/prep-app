"""The browser the goldens were captured with, pinned beside them.

A Playwright bump moves glyph rasterization by a device pixel, which
the comparator reads as a regression in the app. `goldens/browser.txt`
names the browser every golden came from; a compare run on any other
browser fails before its first shot, and golden mode rewrites the pin.
"""

from __future__ import annotations

from pathlib import Path

from tests.parity.harness.constants import GOLDENS_ROOT

BROWSER_FILE = GOLDENS_ROOT / "browser.txt"


def browser_label(browser) -> str:
    return f"{browser.browser_type.name} {browser.version}"


def read_pin(path: Path = BROWSER_FILE) -> str | None:
    return path.read_text(encoding="utf-8").strip() if path.is_file() else None


def write_pin(label: str, path: Path = BROWSER_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(label + "\n", encoding="utf-8")


def check_pin(recorded: str | None, actual: str, mode: str) -> str | None:
    """The reason a compare run must not proceed, or None. Golden mode
    and an unpinned golden set always proceed."""
    if mode == "golden" or recorded is None or recorded == actual:
        return None
    return (
        f"the goldens were captured with {recorded!r}; this run has {actual!r}. "
        "Install the pinned Playwright browser, or re-golden every flow "
        "(PARITY_MODE=golden) to move the pin."
    )
