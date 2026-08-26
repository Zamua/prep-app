"""Steps shared by the flow modules."""

from __future__ import annotations

import time

from tests.parity.harness.capture import SwapWaiter
from tests.parity.harness.registry import FlowCtx

# The IndexedDB rows the offline app renders from.
_IDB_META_GET = """
async (name) => new Promise((resolve) => {
  const req = indexedDB.open("prep-offline");
  req.onsuccess = () => {
    const db = req.result;
    if (!db.objectStoreNames.contains("meta")) { db.close(); resolve(null); return; }
    const get = db.transaction("meta", "readonly").objectStore("meta").get(name);
    get.onsuccess = () => { db.close(); resolve(get.result ?? null); };
    get.onerror = () => { db.close(); resolve(null); };
  };
  req.onerror = () => resolve(null);
})
"""

_IDB_COUNT = """
async (store) => new Promise((resolve) => {
  const req = indexedDB.open("prep-offline");
  req.onsuccess = () => {
    const db = req.result;
    if (!db.objectStoreNames.contains(store)) { db.close(); resolve(0); return; }
    const count = db.transaction(store, "readonly").objectStore(store).count();
    count.onsuccess = () => { db.close(); resolve(count.result); };
    count.onerror = () => { db.close(); resolve(0); };
  };
  req.onerror = () => resolve(0);
})
"""


def fresh_swap() -> SwapWaiter:
    """The waiter for a document about to be loaded: its swap counter
    starts at zero, so the first `htmx:afterSwap` satisfies it."""
    return SwapWaiter(baseline=0)


def open_page(ctx: FlowCtx, path: str, selector: str) -> SwapWaiter:
    """Navigate to `path` and wait for `selector`; a shot passing the
    returned waiter lands after the masthead badge's first swap."""
    ctx.page.goto(ctx.url(path), wait_until="load")
    ctx.page.wait_for_selector(selector)
    return fresh_swap()


def click_and_wait(ctx: FlowCtx, selector: str, then: str) -> None:
    """Click `selector` and wait for `then` to appear."""
    ctx.page.click(selector)
    ctx.page.wait_for_selector(then)


def wait_for_snapshot(ctx: FlowCtx, *, cards: int, timeout_ms: int = 20_000) -> None:
    """Block until the offline snapshot has an owner and at least
    `cards` cards in IndexedDB.

    Polled from here rather than through `wait_for_function`, which reports
    an async predicate satisfied before its promise resolves and so waits
    for nothing: the flow would navigate away mid-write and render an empty
    shell. Only a target slow enough to lose the race shows it.
    """
    page = ctx.page
    deadline = time.monotonic() + timeout_ms / 1000
    owner, count = None, 0
    while True:
        owner = page.evaluate(
            "async () => { const f = " + _IDB_META_GET + "; return await f('owner'); }"
        )
        count = page.evaluate(
            "async (s) => { const f = " + _IDB_COUNT + "; return await f(s); }", "cards"
        )
        if owner is not None and count >= cards:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"offline snapshot not ready: owner={owner!r}, cards={count} < {cards}"
            )
        page.wait_for_timeout(100)
