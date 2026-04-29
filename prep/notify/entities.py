"""Entities for the notify bounded context."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class NotifyMode(str, Enum):
    """How the user wants to be pinged.

    - off:        no notifications at all
    - digest:     one push per day at the chosen hour (local tz)
    - when-ready: push when due-card count crosses a threshold
    """

    OFF = "off"
    DIGEST = "digest"
    WHEN_READY = "when-ready"


class NotificationPrefs(BaseModel):
    """User-controlled notification preferences. Stored as JSON on
    users.notification_prefs; merged over the canonical defaults at
    read time so new keys land safely."""

    mode: NotifyMode = NotifyMode.OFF
    digest_hour: int = Field(9, ge=0, le=23)
    tz: str = Field("America/New_York", max_length=64)
    threshold: int = Field(3, ge=1, le=99)
    quiet_hours_enabled: bool = False
    quiet_start_hour: int = Field(22, ge=0, le=23)
    quiet_end_hour: int = Field(8, ge=0, le=23)
    # Scheduler-managed state — never set via the API; the scheduler
    # writes them after a fire to dedupe future ticks.
    last_digest_date: str | None = None
    last_when_ready_at: str | None = None


class PushSubscription(BaseModel):
    """A single browser/device subscription for web push."""

    endpoint: str
    user_id: str
    p256dh: str
    auth: str
    created_at: str
    last_seen_at: str
