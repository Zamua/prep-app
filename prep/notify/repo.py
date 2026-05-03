"""Repositories for the notify bounded context.

Two responsibilities:
- NotifyPrefsRepo — user notification preferences (a JSON blob on
  the users table).
- PushSubsRepo    — per-device push subscriptions.

Both are facades over the existing prep.db accessors for now,
returning entities at the boundary.
"""

from __future__ import annotations

from datetime import datetime, timezone

from prep import db as _legacy_db
from prep.infrastructure.db import cursor
from prep.notify.entities import (
    NotificationLogEntry,
    NotificationPrefs,
    NotifyMode,
    PushSubscription,
)


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


class NotificationLogRepo:
    """Persisted history of every push we sent. Lets the user find a
    notification that was dismissed/missed/glitched. Cheap append-only
    table; one row per push fired by `send_to_user`."""

    def append(self, *, user_id: str, title: str, body: str, url: str, source: str) -> int:
        """Insert one row, return its id. Called from send_to_user
        regardless of push delivery success/failure — the user might
        still want to see what was *attempted*."""
        sent_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with cursor() as c:
            cur = c.execute(
                """INSERT INTO notifications_log
                       (user_id, sent_at, title, body, url, source)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, sent_at, title, body, url, source),
            )
            return int(cur.lastrowid)

    def list_recent(self, user_id: str, limit: int = 50) -> list[NotificationLogEntry]:
        """Most-recent first. UI page renders this; cap modest so we
        don't ship an unbounded list to the browser."""
        with cursor() as c:
            rows = c.execute(
                """SELECT id, user_id, sent_at, title, body, url, source, seen_at
                     FROM notifications_log
                    WHERE user_id = ?
                    ORDER BY sent_at DESC
                    LIMIT ?""",
                (user_id, limit),
            ).fetchall()
        return [NotificationLogEntry.model_validate(dict(r)) for r in rows]

    def count_unseen(self, user_id: str) -> int:
        """For the masthead badge: how many notifications have arrived
        since the user last opened the log."""
        with cursor() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM notifications_log "
                "WHERE user_id = ? AND seen_at IS NULL",
                (user_id,),
            ).fetchone()
        return int(row["n"] or 0)

    def mark_all_seen(self, user_id: str) -> None:
        """Call when the user opens /notify/log — clears the unread
        badge for every previously-unseen entry."""
        seen_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with cursor() as c:
            c.execute(
                "UPDATE notifications_log SET seen_at = ? " "WHERE user_id = ? AND seen_at IS NULL",
                (seen_at, user_id),
            )


__all__ = [
    "NotificationLogEntry",
    "NotificationLogRepo",
    "NotifyMode",
    "NotifyPrefsRepo",
    "PushSubsRepo",
]
