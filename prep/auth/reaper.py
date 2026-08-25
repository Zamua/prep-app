"""Deleting anonymous accounts that stopped coming back.

An anonymous account is minted without anyone asking for one, so
nothing ever deletes it on the person's behalf. This sweep is that
delete: anonymous rows only, idle past the retention window only, in
batches small enough that no tick holds the write lock for long.

It walks `prep.auth.merge.POLICY` rather than a list of its own. The
merge and the reap destroy the same rows, so a user-owned table added
later must be impossible to remember in one place and forget in the
other, and the boot guard that keeps that map honest then covers both.

The idle window deliberately exceeds the cookie's `MAX_AGE_SECONDS`:
a cookie that still verifies always names a live row, so "valid
cookie, missing user" stays a rare path rather than a routine one.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from prep.auth.merge import POLICY
from prep.infrastructure import clock
from prep.infrastructure.db import cursor

log = logging.getLogger(__name__)

IDLE_DAYS = 365

# Per tick, not per day: no state survives a restart, so a throttle
# would be a claim with nothing behind it while a deploy loop re-ran
# the sweep on every boot. At a 5-minute tick this clears 14,400
# accounts a day against a mint ceiling of 200.
BATCH_LIMIT = 50

_SELECT_BATCH = (
    "SELECT tailscale_login FROM users"
    " WHERE is_anonymous = 1 AND last_seen_at < ?"
    " ORDER BY last_seen_at LIMIT ?"
)

# Anonymous AND idle, in one predicate, applied again inside the
# per-account transaction. Strict `<`: a row written at exactly the
# cutoff instant survives.
_ELIGIBLE = "tailscale_login = ? AND is_anonymous = 1 AND last_seen_at < ?"


def cutoff_for(now_utc: datetime) -> str:
    """`last_seen_at` is TEXT written by `db.now()`, so the comparison
    is a string sort and the cutoff must carry the same `+00:00`
    suffix. A `Z`-suffixed cutoff sorts ABOVE an equal-instant
    `+00:00` value and deletes rows it should keep; a naive one sorts
    below every value and silently reaps nothing."""
    if now_utc.tzinfo is None:
        raise ValueError("reaper cutoff needs an aware UTC datetime")
    return (now_utc - timedelta(days=IDLE_DAYS)).isoformat()


def reap_idle_anonymous(now_utc: datetime | None = None) -> int:
    """Delete at most `BATCH_LIMIT` idle anonymous accounts and return
    how many went. Blocking sqlite work: callers on the event loop run
    it through `asyncio.to_thread`.

    One transaction per account, so request traffic interleaves
    between accounts instead of waiting out the whole batch."""
    cutoff = cutoff_for(now_utc or clock.now())
    with cursor() as c:
        candidates = [
            row["tailscale_login"]
            for row in c.execute(_SELECT_BATCH, (cutoff, BATCH_LIMIT)).fetchall()
        ]
    reaped = 0
    failed = 0
    for user_id in candidates:
        # One account's failure must not abort the sweep: each has its
        # own transaction, the next tick re-selects whatever is left,
        # and a destroyed account still owes the operator a log line.
        try:
            reaped += _reap_one(user_id, cutoff)
        except Exception:
            failed += 1
            log.exception("reaping anonymous account %s failed", user_id)
    if reaped or failed:
        log.info("reaped %d idle anonymous account(s), %d failed", reaped, failed)
    return reaped


def _reap_one(user_id: str, cutoff: str) -> int:
    """Destroy one account's rows and the account. The eligibility
    re-read and every delete share one write lock: the batch was
    selected outside it, and a visitor returning in between bumps
    `last_seen_at` while a sign-in merges the row away.

    Every policy table is deleted explicitly rather than only the four
    with no FK to `users`. The FK chain would cascade the rest, but a
    table added later without one is then destroyed by the same loop
    instead of surviving as an orphan."""
    deleted = 0
    with cursor() as c:
        c.execute("BEGIN IMMEDIATE")
        if c.execute(f"SELECT 1 FROM users WHERE {_ELIGIBLE}", (user_id, cutoff)).fetchone():
            for rule in POLICY:
                c.execute(f'DELETE FROM "{rule.table}" WHERE "{rule.column}" = ?', (user_id,))
            deleted = c.execute(f"DELETE FROM users WHERE {_ELIGIBLE}", (user_id, cutoff)).rowcount
    return deleted
