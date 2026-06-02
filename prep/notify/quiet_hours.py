"""Quiet-hours helper shared between the SRS notify scheduler and the
trivia scheduler. Both schedulers honor the same per-user prefs
(`quiet_hours_enabled`, `quiet_start_hour`, `quiet_end_hour`, `tz`)
with the same wrap-midnight semantics — having two copies was an
easy way to drift on a default change.

Defaults: 22:00 → 08:00 local time, America/New_York if the user
hasn't set a tz. The wrap-midnight branch is the common case
(quiet at night).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

_DEFAULT_TZ = "America/New_York"
_DEFAULT_START = 22
_DEFAULT_END = 8


def in_quiet_hours(local_hour: int, quiet_start: int, quiet_end: int) -> bool:
    """Pure check: is `local_hour` within the [start, end) quiet window?

    Handles the wrap-midnight case (e.g. 22..8 means quiet from 22:00
    through 08:00). `start == end` means no quiet hours; returns False
    so a misconfigured equal range doesn't silence everything.
    """
    if quiet_start == quiet_end:
        return False
    if quiet_start < quiet_end:
        return quiet_start <= local_hour < quiet_end
    return local_hour >= quiet_start or local_hour < quiet_end


def should_silence(prefs: dict, now_utc: datetime) -> bool:
    """Should we suppress a notification for THIS user RIGHT NOW?

    Returns False when quiet-hours is disabled OR when the current
    hour in the user's local tz falls outside the quiet window.
    Defensive on bad tz strings (falls back to the default), so a
    typo in prefs.tz can't crash the scheduler tick.
    """
    if not prefs.get("quiet_hours_enabled"):
        return False
    try:
        tz = ZoneInfo(prefs.get("tz") or _DEFAULT_TZ)
    except Exception:  # noqa: BLE001 — unknown / malformed tz string
        tz = ZoneInfo(_DEFAULT_TZ)
    local_hour = now_utc.astimezone(tz).hour
    start = int(prefs.get("quiet_start_hour", _DEFAULT_START))
    end = int(prefs.get("quiet_end_hour", _DEFAULT_END))
    return in_quiet_hours(local_hour, start, end)
