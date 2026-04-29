"""Repositories for the notify bounded context.

Two responsibilities:
- NotifyPrefsRepo — user notification preferences (a JSON blob on
  the users table).
- PushSubsRepo    — per-device push subscriptions.

Both are facades over the existing prep.db accessors for now,
returning entities at the boundary.
"""

from __future__ import annotations

from prep import db as _legacy_db
from prep.notify.entities import NotificationPrefs, NotifyMode, PushSubscription


class NotifyPrefsRepo:
    """Read/write access to per-user notification preferences."""

    def get(self, user_id: str) -> NotificationPrefs:
        """Always returns a NotificationPrefs (defaults populate for
        users who've never opened settings)."""
        raw = _legacy_db.get_notification_prefs(user_id)
        return NotificationPrefs.model_validate(raw)

    def set(self, user_id: str, prefs: NotificationPrefs) -> None:
        _legacy_db.set_notification_prefs(user_id, prefs.model_dump())


class PushSubsRepo:
    """Read/write access to push_subscriptions."""

    def upsert(self, user_id: str, endpoint: str, p256dh: str, auth: str) -> None:
        _legacy_db.upsert_push_subscription(user_id, endpoint, p256dh, auth)

    def list_for_user(self, user_id: str) -> list[PushSubscription]:
        rows = _legacy_db.list_push_subscriptions(user_id)
        return [PushSubscription.model_validate(r) for r in rows]

    def count_for_user(self, user_id: str) -> int:
        return len(_legacy_db.list_push_subscriptions(user_id))

    def delete_by_endpoint(self, endpoint: str) -> None:
        _legacy_db.delete_push_subscription(endpoint)


__all__ = [
    "NotifyMode",
    "NotifyPrefsRepo",
    "PushSubsRepo",
]
