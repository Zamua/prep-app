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


# ---- job screens (phase 4) -------------------------------------------------


def wait_status(ctx: FlowCtx, status: str, *, timeout_ms: int = 30_000) -> None:
    """Block until the progress partial renders `status`. The partial
    stamps it on `#t-status` / `#trivia-gen-status` as `data-status`."""
    ctx.page.wait_for_selector(f'[data-status="{status}"]', timeout=timeout_ms)


def wait_badge(ctx: FlowCtx, label: str, *, timeout_ms: int = 30_000) -> None:
    """Block until the masthead badge's own 5s poll has caught up with
    the jobs behind it. The chip's aria-label carries both counts, so
    waiting on it pins the icon and the number a shot will hold."""
    ctx.page.wait_for_selector(f'#workflow-badge summary[aria-label="{label}"]', timeout=timeout_ms)


def submit_form(ctx: FlowCtx, selector: str, then: str) -> None:
    """Click a form's submit and wait out the navigation it starts."""
    with ctx.page.expect_navigation(wait_until="load"):
        ctx.page.click(selector)
    ctx.page.wait_for_selector(then)


def wait_badge_status(ctx: FlowCtx, status: str, *, timeout_ms: int = 30_000) -> None:
    """Block until the badge's row carries `status`.

    Only a progress fragment's own poll writes the tracked status, so a
    flow that leaves a job's page before that poll lands would carry the
    status the start registered into every later shot.
    """
    ctx.page.wait_for_function(
        "s => document.querySelector('#workflow-badge .workflow-row-status')"
        "?.textContent.includes(s)",
        arg=status,
        timeout=timeout_ms,
    )


def wait_badge_empty(ctx: FlowCtx, *, timeout_ms: int = 30_000) -> None:
    """Block until the badge reports no tracked workflows."""
    # Attached, not visible: an empty badge renders a zero-size div.
    ctx.page.wait_for_selector(
        '#workflow-badge[data-empty="1"]', state="attached", timeout=timeout_ms
    )


_FRAGMENT_GLOB = "**/fragment"


def freeze_polling(ctx: FlowCtx) -> None:
    """Abort the progress fragment's polls so a transient state holds.

    A post-signal state (`applying`, `rejecting`) lives only until the
    next 2s poll, which is less than a full-page capture takes. htmx
    leaves the DOM alone when a request fails and the stylesheet keys off
    none of its state classes, so the frozen screen is the one the server
    sent. The badge keeps polling on its own URL.
    """
    ctx.page.route(_FRAGMENT_GLOB, lambda route: route.abort())


def resume_polling(ctx: FlowCtx) -> None:
    ctx.page.unroute(_FRAGMENT_GLOB)


def shot(ctx: FlowCtx, label: str, **kwargs):
    """Scroll to the top, then capture.

    The masthead is sticky and the install nudge is fixed, so a full-page
    capture draws both wherever the page happens to be scrolled. A job
    page's height changes under the reader as fragments swap, which moves
    that offset between runs.
    """
    ctx.page.evaluate("() => window.scrollTo(0, 0)")
    return ctx.shot(label, **kwargs)
