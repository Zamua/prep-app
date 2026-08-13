"""Persistence for the instant bounded context.

`check_and_reserve` is the single admission gate. Every window count
and the `pending` row INSERT run inside one immediate transaction, so
two concurrent requests at count N-1 admit exactly one: the reserved
row IS what the next request's count sees.

Outcome classes decide what counts. `pending`, `ok`, and
`failed_spent` represent upstream token spend (pending is in-flight
spend until resolved) and count toward every quota window.
`failed_free` rows are no-spend refusals and count toward the per-IP
burst window only.

The per-IP windows are the anti-Sybil lever: clearing cookies buys no
fresh budget. The per-user windows are the anti-NAT lever: a stranger
behind the same carrier CGNAT cannot spend a signed-in user's budget.
Neither alone is sufficient, so both must pass.

`create_instant_deck` is the other write: the account, the deck and
its cards in ONE transaction, because a `users` row without its first
deck, or a deck missing half its cards, are the states a generated
deck must never land in.
"""

from __future__ import annotations

import math
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from prep.auth.anon_cookie import ID_BYTES, external_id_from_bytes
from prep.auth.limits import refuse_over_row_cap
from prep.auth.providers.anon import ANON_DISPLAY_NAME
from prep.decks.entities import SLUG_ALPHABET, SLUG_LENGTH
from prep.infrastructure.db import cursor, now

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
DEFAULT_PER_ANON_USER_PER_DAY = 3
DEFAULT_PER_USER_PER_DAY = 20
DEFAULT_GLOBAL_PER_DAY = 200
DEFAULT_GLOBAL_PER_MINUTE = 4

_ENV_BURST_LIMIT = "PREP_INSTANT_BURST_LIMIT"
_ENV_BURST_WINDOW_S = "PREP_INSTANT_BURST_WINDOW_S"
_ENV_PER_IP_PER_DAY = "PREP_INSTANT_PER_IP_PER_DAY"
_ENV_PER_ANON_USER_PER_DAY = "PREP_INSTANT_PER_ANON_USER_PER_DAY"
_ENV_PER_USER_PER_DAY = "PREP_INSTANT_PER_USER_PER_DAY"
_ENV_GLOBAL_PER_DAY = "PREP_INSTANT_GLOBAL_PER_DAY"
_ENV_GLOBAL_PER_MINUTE = "PREP_INSTANT_GLOBAL_PER_MINUTE"

_SPEND_OUTCOMES = ("pending", "ok", "failed_spent")
_SPEND_MARKS = ",".join("?" for _ in _SPEND_OUTCOMES)

_TERMINAL_OUTCOMES = ("ok", "failed_spent", "failed_free")

# A slug is 40 bits over a per-user namespace, so the bound is only
# reached when the generator is broken; failing loudly beats looping.
_MAX_SLUG_ATTEMPTS = 100


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


@dataclass(frozen=True)
class InstantDeckResult:
    """The stored deck. `minted` is True when this write created the
    account, which is what tells the caller to set a cookie."""

    user_id: str
    slug: str
    minted: bool


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


def _day_window(
    c, column: str, value: str, *, day_cutoff: str, limit: int, at: datetime
) -> Refusal | None:
    """One rolling-24h count of spend outcomes, keyed by `ip` or
    `user_id`. The window admits again once enough of the oldest
    counted rows age out to bring the count under the limit."""
    n = c.execute(
        f'SELECT COUNT(*) AS n FROM instant_generations WHERE "{column}" = ?'
        f" AND created_at >= ? AND outcome IN ({_SPEND_MARKS})",
        (value, day_cutoff, *_SPEND_OUTCOMES),
    ).fetchone()["n"]
    if n < limit:
        return None
    oldest = c.execute(
        f'SELECT created_at FROM instant_generations WHERE "{column}" = ?'
        f" AND created_at >= ? AND outcome IN ({_SPEND_MARKS})"
        " ORDER BY created_at ASC LIMIT 1 OFFSET ?",
        (value, day_cutoff, *_SPEND_OUTCOMES, n - limit),
    ).fetchone()
    return Refusal("day", _retry_after(at, oldest["created_at"] if oldest else None, DAY_WINDOW_S))


def _user_day_limit(c, user_id: str) -> int:
    """An anonymous account costs one generation to obtain, so it gets
    the tighter budget. A user id naming no row takes it too."""
    row = c.execute(
        "SELECT is_anonymous FROM users WHERE tailscale_login = ?", (user_id,)
    ).fetchone()
    if row is None or row["is_anonymous"]:
        return _env_int(_ENV_PER_ANON_USER_PER_DAY, DEFAULT_PER_ANON_USER_PER_DAY)
    return _env_int(_ENV_PER_USER_PER_DAY, DEFAULT_PER_USER_PER_DAY)


def check_and_reserve(
    ip: str, *, topic_chars: int, user_id: str | None = None, at: datetime | None = None
) -> Reservation | Refusal:
    """Check every limiter window and, if all pass, insert the
    `pending` reservation row - one transaction. Rows older than the
    retention window are pruned opportunistically here.

    `user_id` is the account the spend is charged to, or None when the
    request has no account yet: the one that mints it is back-stamped
    by `resolve`."""
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
        refusal = _day_window(c, "ip", ip, day_cutoff=day_cutoff, limit=per_ip_per_day, at=at)
        if refusal is not None:
            return refusal

        # Per-user daily. Skipped only for the request that mints an
        # account, which has no id to count against yet; `resolve`
        # back-stamps that row, so it counts from that moment on.
        if user_id is not None:
            refusal = _day_window(
                c,
                "user_id",
                user_id,
                day_cutoff=day_cutoff,
                limit=_user_day_limit(c, user_id),
                at=at,
            )
            if refusal is not None:
                return refusal

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
            "INSERT INTO instant_generations (ip, created_at, outcome, topic_chars, user_id)"
            " VALUES (?, ?, 'pending', ?, ?)",
            (ip, at.isoformat(), topic_chars, user_id),
        )
        return Reservation(id=cur.lastrowid)


def resolve(
    reservation_id: int, outcome: str, *, cards: int | None = None, user_id: str | None = None
) -> None:
    """Resolve a reservation's outcome class when the request
    completes. An unresolved row stays `pending`, which counts as
    spend: crashes fail closed.

    `user_id` stamps the account for the request that minted one. It
    never clears an id the reservation already carries."""
    if outcome not in _TERMINAL_OUTCOMES:
        raise ValueError(f"unknown outcome: {outcome!r}")
    with cursor() as c:
        c.execute(
            "UPDATE instant_generations SET outcome = ?, cards = ?,"
            " user_id = COALESCE(?, user_id) WHERE id = ?",
            (outcome, cards, user_id, reservation_id),
        )


def _free_slug(c, user_id: str) -> str:
    """An opaque slug free within this user's namespace, drawn and
    checked on the transaction's own connection."""
    for _ in range(_MAX_SLUG_ATTEMPTS):
        candidate = "".join(secrets.choice(SLUG_ALPHABET) for _ in range(SLUG_LENGTH))
        taken = c.execute(
            "SELECT 1 FROM decks WHERE user_id = ? AND name = ?", (user_id, candidate)
        ).fetchone()
        if taken is None:
            return candidate
    raise RuntimeError(f"no free deck slug after {_MAX_SLUG_ATTEMPTS} attempts")


def create_instant_deck(
    *,
    user_id: str | None,
    display_name: str,
    cards: list[dict],
) -> InstantDeckResult:
    """Store a generated deck: the account when there is none yet, the
    deck, and every card - ONE transaction.

    `user_id` None mints an anonymous account. The cards arrive
    validated by the service, so nothing inside here can reject one:
    a partial deck and an account with no deck are the two states this
    method exists to make unreachable.

    Raises `RowCapReached` when the account is at its anonymous row
    cap, before anything is written."""
    ts = now()
    minted = user_id is None
    with cursor() as c:
        # IMMEDIATE takes the write lock up front, so the cap count and
        # the inserts it guards are serialized against a concurrent
        # generation from the same account.
        c.execute("BEGIN IMMEDIATE")
        if user_id is None:
            user_id = external_id_from_bytes(secrets.token_bytes(ID_BYTES))
            c.execute(
                "INSERT INTO users (tailscale_login, display_name, email, created_at,"
                " last_seen_at, is_anonymous) VALUES (?, ?, NULL, ?, ?, 1)",
                (user_id, ANON_DISPLAY_NAME, ts, ts),
            )
        else:
            refuse_over_row_cap(c, user_id, new_decks=1, new_questions=len(cards))
        slug = _free_slug(c, user_id)
        deck = c.execute(
            "INSERT INTO decks (user_id, name, display_name, created_at) VALUES (?, ?, ?, ?)",
            (user_id, slug, display_name, ts),
        )
        deck_id = deck.lastrowid
        for card in cards:
            question = c.execute(
                "INSERT INTO questions"
                " (user_id, deck_id, type, prompt, answer, answer_regex, created_at)"
                " VALUES (?, ?, 'short', ?, ?, ?, ?)",
                (user_id, deck_id, card["prompt"], card["answer"], card["answer_regex"], ts),
            )
            c.execute(
                "INSERT INTO cards (question_id, step, next_due) VALUES (?, 0, ?)",
                (question.lastrowid, ts),
            )
    return InstantDeckResult(user_id=user_id, slug=slug, minted=minted)
