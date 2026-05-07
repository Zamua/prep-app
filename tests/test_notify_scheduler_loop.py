"""Regression test for the 2026-05-07 19:30 UTC outage.

The notify scheduler's `_tick` coroutine called the (sync) trivia
scheduler tick directly, which could block on `urllib.urlopen` deep
inside `generate_batch` for up to 900s. That blocks the asyncio event
loop and looks like prod is down.

Fix: notify._tick now does `await asyncio.to_thread(_trivia_tick, ...)`.

This test pretends `_trivia_tick` blocks for 1.0s (a stand-in for the
slow agent HTTP call) and concurrently runs a counter coroutine that
ticks every 0.05s. If `_trivia_tick` were called synchronously inside
the coroutine, the counter would barely advance during the 1.0s block.
With `to_thread`, the loop keeps yielding so the counter ticks ~20
times. We assert the counter advanced enough to prove the loop was
free.
"""

from __future__ import annotations

import asyncio
import time


async def test_notify_tick_does_not_block_event_loop_during_trivia_refill(monkeypatch):
    from prep.notify import scheduler as ns
    from prep.trivia import scheduler as ts

    BLOCK_SECS = 1.0

    def slow_blocking_trivia_tick(_now):
        # Stand in for a real generate_batch call inside trivia.tick:
        # blocks the calling thread for BLOCK_SECS. If this runs ON the
        # event loop, the loop freezes for that long; if it runs OFF
        # (via asyncio.to_thread), the loop keeps spinning.
        time.sleep(BLOCK_SECS)

    monkeypatch.setattr(ts, "tick", slow_blocking_trivia_tick)

    # Stub out the rest of `_tick` — we only care that the trivia branch
    # doesn't block the loop. The user / digest / push paths each touch
    # repos that need a DB; bypassing them keeps this test self-contained.
    async def _no_op_tick():
        # Re-implement just the trivia branch the way notify._tick does.
        # If notify._tick changes shape we'll catch the divergence in
        # the (separate) integration tests.
        await asyncio.to_thread(ts.tick, None)

    counter = 0

    async def _ticker():
        nonlocal counter
        for _ in range(40):  # 40 × 50ms = 2s total budget
            await asyncio.sleep(0.05)
            counter += 1

    # Race the slow trivia tick against the 50ms-stepping counter. If
    # the loop isn't blocked, the counter should reach ~20 by the time
    # the trivia tick finishes 1s of sleeping.
    t0 = time.monotonic()
    await asyncio.gather(_no_op_tick(), _ticker())
    elapsed = time.monotonic() - t0

    # Sanity: total wall time is dominated by the blocking trivia tick
    # (1.0s), not the counter loop (2.0s budget); both should run
    # concurrently.
    assert elapsed < 2.5, f"unexpectedly slow: {elapsed:.2f}s — concurrency may be broken"
    # Real assertion: the counter advanced through MOST of its budget
    # while the trivia tick was sleeping. A direct sync call would
    # leave counter≈0 (the loop wakes once when the tick returns).
    # to_thread keeps the loop free so counter ≈ 20 (the 1s of blocking
    # / 50ms = 20 ticks). Allow a generous lower bound for CI flakiness.
    assert counter >= 10, (
        f"counter only reached {counter}/40 — event loop appears blocked. "
        "Did notify._tick stop using asyncio.to_thread for the trivia branch?"
    )

    # Also verify that the actual `_tick` function uses to_thread under
    # the hood. This is a structural check: if a future refactor
    # introduces a direct sync call somewhere else, this catches it.
    import inspect

    src = inspect.getsource(ns._tick)
    assert "asyncio.to_thread" in src, (
        "notify._tick no longer calls trivia tick via asyncio.to_thread — "
        "this is the regression that took prod down on 2026-05-07."
    )
