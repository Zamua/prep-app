"""instant_generations ledger access: the rate limiter's storage.

`check_and_reserve` is the single admission gate. Every window count
and the `pending` row INSERT run inside one immediate transaction, so
two concurrent requests at count N-1 admit exactly one: the reserved
row IS what the next request's count sees.

Outcome classes decide what counts. `pending`, `ok`, and
`failed_spent` represent upstream token spend (pending is in-flight
spend until resolved) and count toward every quota window.
`failed_free` rows are no-spend refusals and count toward the per-IP
burst window only.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from prep.infrastructure.db import cursor

# The global per-minute cap's window is fixed: it defines what "per
# minute" means. The per-IP burst window is env-tunable like the
# other limits.
MINUTE_WINDOW_S = 60
DAY_WINDOW_S = 86400
RETENTION_DAYS = 7

# Per-IP burst: at most this many rows (any outcome) per window.
DEFAULT_BURST_LIMIT = 1
DEFAULT_BURST_WINDOW_S = 60
DEFAULT_PER_IP_PER_DAY = 3
DEFAULT_GLOBAL_PER_DAY = 200
DEFAULT_GLOBAL_PER_MINUTE = 4

_ENV_BURST_LIMIT = "PREP_INSTANT_BURST_LIMIT"
_ENV_BURST_WINDOW_S = "PREP_INSTANT_BURST_WINDOW_S"
_ENV_PER_IP_PER_DAY = "PREP_INSTANT_PER_IP_PER_DAY"
_ENV_GLOBAL_PER_DAY = "PREP_INSTANT_GLOBAL_PER_DAY"
_ENV_GLOBAL_PER_MINUTE = "PREP_INSTANT_GLOBAL_PER_MINUTE"

_SPEND_OUTCOMES = ("pending", "ok", "failed_spent")
_SPEND_MARKS = ",".join("?" for _ in _SPEND_OUTCOMES)

_TERMINAL_OUTCOMES = ("ok", "failed_spent", "failed_free")


@dataclass(frozen=True)
class Reservation:
    """Admitted attempt. The caller must resolve the row's outcome via
    `resolve` when the request completes."""

    id: int


@dataclass(frozen=True)
class Refusal:
    """Refused attempt; no row was inserted. `kind` values `minute`
    and `day` are rate_limited scopes; `busy` is a tripped global
    cap. `retry_after_s` accompanies the rate_limited kinds."""

    kind: str
    retry_after_s: int | None = None


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _retry_after(at: datetime, created_at_iso: str | None, window_s: int) -> int:
    """Seconds until the given row ages out of its window."""
    if not created_at_iso:
        return window_s
    try:
        ts = datetime.fromisoformat(created_at_iso)
    except ValueError:
        return window_s
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    remaining = window_s - (at - ts).total_seconds()
    return max(1, math.ceil(remaining))


def check_and_reserve(
    ip: str, *, topic_chars: int, at: datetime | None = None
) -> Reservation | Refusal:
    """Check every limiter window and, if all pass, insert the
    `pending` reservation row - one transaction. Rows older than the
    retention window are pruned opportunistically here."""
    at = at or datetime.now(timezone.utc)
    burst_limit = _env_int(_ENV_BURST_LIMIT, DEFAULT_BURST_LIMIT)
    burst_window_s = _env_int(_ENV_BURST_WINDOW_S, DEFAULT_BURST_WINDOW_S)
    burst_cutoff = (at - timedelta(seconds=burst_window_s)).isoformat()
    minute_cutoff = (at - timedelta(seconds=MINUTE_WINDOW_S)).isoformat()
    day_cutoff = (at - timedelta(seconds=DAY_WINDOW_S)).isoformat()
    prune_cutoff = (at - timedelta(days=RETENTION_DAYS)).isoformat()
    per_ip_per_day = _env_int(_ENV_PER_IP_PER_DAY, DEFAULT_PER_IP_PER_DAY)
    global_per_day = _env_int(_ENV_GLOBAL_PER_DAY, DEFAULT_GLOBAL_PER_DAY)
    global_per_minute = _env_int(_ENV_GLOBAL_PER_MINUTE, DEFAULT_GLOBAL_PER_MINUTE)

    with cursor() as c:
        # IMMEDIATE takes the write lock up front so the counts and
        # the INSERT are serialized against concurrent reservations.
        c.execute("BEGIN IMMEDIATE")
        c.execute("DELETE FROM instant_generations WHERE created_at < ?", (prune_cutoff,))

        # Per-IP burst: every row counts, whatever its outcome.
        burst = c.execute(
            "SELECT COUNT(*) AS n, MAX(created_at) AS newest"
            " FROM instant_generations WHERE ip = ? AND created_at >= ?",
            (ip, burst_cutoff),
        ).fetchone()
        if burst["n"] >= burst_limit:
            return Refusal("minute", _retry_after(at, burst["newest"], burst_window_s))

        # Per-IP daily: spend outcomes only.
        row = c.execute(
            "SELECT COUNT(*) AS n FROM instant_generations"
            f" WHERE ip = ? AND created_at >= ? AND outcome IN ({_SPEND_MARKS})",
            (ip, day_cutoff, *_SPEND_OUTCOMES),
        ).fetchone()
        if row["n"] >= per_ip_per_day:
            # The window admits again once enough of the oldest counted
            # rows age out to bring the count under the limit.
            oldest = c.execute(
                "SELECT created_at FROM instant_generations"
                f" WHERE ip = ? AND created_at >= ? AND outcome IN ({_SPEND_MARKS})"
                " ORDER BY created_at ASC LIMIT 1 OFFSET ?",
                (ip, day_cutoff, *_SPEND_OUTCOMES, row["n"] - per_ip_per_day),
            ).fetchone()
            created = oldest["created_at"] if oldest else None
            return Refusal("day", _retry_after(at, created, DAY_WINDOW_S))

        # Global per-minute cap: rations the shared per-minute token
        # budget across the deploy.
        row = c.execute(
            "SELECT COUNT(*) AS n FROM instant_generations"
            f" WHERE created_at >= ? AND outcome IN ({_SPEND_MARKS})",
            (minute_cutoff, *_SPEND_OUTCOMES),
        ).fetchone()
        if row["n"] >= global_per_minute:
            return Refusal("busy")

        # Global daily breaker: bounds the endpoint's share of the free
        # tier's daily quota.
        row = c.execute(
            "SELECT COUNT(*) AS n FROM instant_generations"
            f" WHERE created_at >= ? AND outcome IN ({_SPEND_MARKS})",
            (day_cutoff, *_SPEND_OUTCOMES),
        ).fetchone()
        if row["n"] >= global_per_day:
            return Refusal("busy")

        cur = c.execute(
            "INSERT INTO instant_generations (ip, created_at, outcome, topic_chars)"
            " VALUES (?, ?, 'pending', ?)",
            (ip, at.isoformat(), topic_chars),
        )
        return Reservation(id=cur.lastrowid)


def resolve(reservation_id: int, outcome: str, *, cards: int | None = None) -> None:
    """Resolve a reservation's outcome class when the request
    completes. An unresolved row stays `pending`, which counts as
    spend: crashes fail closed."""
    if outcome not in _TERMINAL_OUTCOMES:
        raise ValueError(f"unknown outcome: {outcome!r}")
    with cursor() as c:
        c.execute(
            "UPDATE instant_generations SET outcome = ?, cards = ? WHERE id = ?",
            (outcome, cards, reservation_id),
        )
