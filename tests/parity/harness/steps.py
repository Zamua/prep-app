"""Steps shared by the flow modules."""

from __future__ import annotations

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
    `cards` cards in IndexedDB."""
    page = ctx.page
    page.wait_for_function(
        "async () => { const f = " + _IDB_META_GET + "; return (await f('owner')) !== null; }",
        timeout=timeout_ms,
    )
    page.wait_for_function(
        "async (n) => { const f = " + _IDB_COUNT + "; return (await f('cards')) >= n; }",
        arg=cards,
        timeout=timeout_ms,
    )
