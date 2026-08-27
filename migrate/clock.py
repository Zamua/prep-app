"""The process clock. Every wall-clock read in the migration tool goes
through here, so one env var pins time for a reproducible export.

`PREP_FAKE_NOW` (ISO-8601 with `Z` or an offset; naive means UTC)
selects a `FixedClock`; unset means the system clock. The provider
resolves lazily on first use and is swappable for tests.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Protocol

ENV_FAKE_NOW = "PREP_FAKE_NOW"

_log = logging.getLogger(__name__)


class Clock(Protocol):
    def now(self) -> datetime:
        """Aware UTC."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    def __init__(self, at: datetime):
        self._at = _to_utc(at)

    def now(self) -> datetime:
        return self._at


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_fake_now(raw: str) -> datetime:
    """`PREP_FAKE_NOW` to an aware UTC instant; malformed raises
    `ValueError` naming the variable."""
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError as e:
        raise ValueError(f"{ENV_FAKE_NOW}={raw!r} is not an ISO-8601 datetime") from e
    return _to_utc(parsed)


_clock: Clock | None = None


def get_clock() -> Clock:
    global _clock
    if _clock is None:
        raw = os.environ.get(ENV_FAKE_NOW, "").strip()
        if raw:
            at = parse_fake_now(raw)
            _log.warning("clock pinned by %s to %s", ENV_FAKE_NOW, at.isoformat())
            _clock = FixedClock(at)
        else:
            _clock = SystemClock()
    return _clock


def set_clock(clock: Clock) -> None:
    global _clock
    _clock = clock


def reset_clock() -> None:
    """Drop the cached provider so the next read re-resolves the env."""
    global _clock
    _clock = None


def now() -> datetime:
    return get_clock().now()


def now_iso(timespec: str = "auto") -> str:
    return now().isoformat(timespec=timespec)


def unix() -> float:
    return now().timestamp()
