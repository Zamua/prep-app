"""Duration parsing for snooze + mute form posts.

Form shape from the bottom-sheet UI: either a `preset` field (a chip
keyword like "1h", "tonight", "tomorrow", "1w", "forever") OR a
`custom` numeric field paired with a `unit` enum ("hours" | "days" |
"weeks"). The route picks one or the other and we resolve to an
ISO-8601 UTC string that the repos can store directly.

Kept in prep.web because both decks/ and study/ + trivia/ routes
consume it — pushing it into any single bounded context would create
import cycles.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

# Sentinel timestamp meaning "effectively forever" — far enough in the
# future that we don't have to special-case None vs forever throughout
# the read path. Lexicographic ISO compares still work, the scheduler
# sees the deck as muted indefinitely. Year 2099 is plenty.
FOREVER_ISO = "2099-12-31T23:59:59+00:00"

_HOURS = {
    "1h": 1,
    "2h": 2,
    "4h": 4,
    "8h": 8,
    "1d": 24,
    "2d": 48,
    "3d": 72,
    "1w": 24 * 7,
    "2w": 24 * 14,
}


def _end_of_day_local(now: datetime) -> datetime:
    """End of "tonight" — 23:59 local. Used by the `tonight` preset."""
    local = now.astimezone()
    end = datetime.combine(local.date(), time(23, 59), tzinfo=local.tzinfo)
    return end.astimezone(timezone.utc)


def _tomorrow_morning_local(now: datetime) -> datetime:
    """8am the next day, local. Used by the `tomorrow` preset."""
    local = now.astimezone()
    nxt = local.date() + timedelta(days=1)
    morning = datetime.combine(nxt, time(8, 0), tzinfo=local.tzinfo)
    return morning.astimezone(timezone.utc)


class DurationError(ValueError):
    """Raised on malformed duration form input. Routes catch this and
    return a 400 with the message."""


def parse_until(
    *,
    preset: str | None,
    custom: str | None,
    unit: str | None,
    now: datetime | None = None,
) -> str:
    """Resolve form params to an ISO-8601 UTC "snooze until" / "mute
    until" timestamp. Either `preset` OR (`custom` + `unit`) must be
    supplied; we prefer `preset` if both are present.

    Presets:
      forever | 1h | 2h | 4h | 8h | 1d | 2d | 3d | 1w | 2w |
      tonight | tomorrow

    Custom: integer 1-999, unit ∈ {hours, days, weeks}. Anything else
    is a DurationError.
    """
    now = now or datetime.now(timezone.utc)
    preset = (preset or "").strip().lower() or None
    if preset:
        if preset == "forever":
            return FOREVER_ISO
        if preset == "tonight":
            return _end_of_day_local(now).isoformat()
        if preset == "tomorrow":
            return _tomorrow_morning_local(now).isoformat()
        hours = _HOURS.get(preset)
        if hours is None:
            raise DurationError(f"unknown preset {preset!r}")
        return (now + timedelta(hours=hours)).isoformat()

    if not custom or not unit:
        raise DurationError("missing preset OR (custom + unit)")
    try:
        n = int(custom)
    except (TypeError, ValueError) as e:
        raise DurationError(f"custom must be an integer, got {custom!r}") from e
    if n < 1 or n > 999:
        raise DurationError(f"custom out of range (1..999): {n}")
    unit_l = unit.strip().lower()
    if unit_l == "hours":
        delta = timedelta(hours=n)
    elif unit_l == "days":
        delta = timedelta(days=n)
    elif unit_l == "weeks":
        delta = timedelta(weeks=n)
    else:
        raise DurationError(f"unknown unit {unit!r}")
    return (now + delta).isoformat()
