"""Repositories for the notify bounded context.

Three responsibilities:
- NotifyPrefsRepo — user notification preferences (a JSON blob on
  the users table; UserRepo owns the actual storage).
- PushSubsRepo    — per-device push subscriptions.
- NotificationLogRepo — append-only log of every push fired.

Plus a `due_breakdown` / `count_due_for_user` helper used by the
scheduler — those were on prep.db before; surfaced here as module
functions because they're pure queries (no entity to model) and the
notify scheduler is the only caller.
"""

from __future__ import annotations

from datetime import datetime, timezone

from prep.auth.repo import UserRepo
from prep.infrastructure.db import cursor, now
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
        raw = UserRepo().get_notification_prefs(user_id)
        return NotificationPrefs.model_validate(raw)

    def set(self, user_id: str, prefs: NotificationPrefs) -> None:
        UserRepo().set_notification_prefs(user_id, prefs.model_dump())


class PushSubsRepo:
    """Read/write access to push_subscriptions."""

    def upsert(self, user_id: str, endpoint: str, p256dh: str, auth: str) -> None:
        ts = now()
        with cursor() as c:
            c.execute(
                """INSERT INTO push_subscriptions (endpoint, user_id, p256dh, auth, created_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(endpoint) DO UPDATE SET
                     user_id = excluded.user_id,
                     p256dh = excluded.p256dh,
                     auth = excluded.auth,
                     last_seen_at = excluded.last_seen_at""",
                (endpoint, user_id, p256dh, auth, ts, ts),
            )

    def list_for_user(self, user_id: str) -> list[PushSubscription]:
        with cursor() as c:
            rows = c.execute(
                "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        return [PushSubscription.model_validate(dict(r)) for r in rows]

    def list_for_user_raw(self, user_id: str) -> list[dict]:
        """Plain dict view of `list_for_user` — used by the push
        sender (push.py) which threads the rows straight into
        pywebpush rather than re-validating each."""
        with cursor() as c:
            rows = c.execute(
                "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_for_user(self, user_id: str) -> int:
        with cursor() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM push_subscriptions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return int(row["n"] or 0)

    def delete_by_endpoint(self, endpoint: str) -> None:
        """Used to prune subscriptions the push service has rejected
        (404/410). Endpoint is the natural unique key; same endpoint
        can only be one user's."""
        with cursor() as c:
            c.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))

    def list_users_with_subs(self) -> list[str]:
        """Return tailscale_login values for every user with at least
        one push subscription. Used by the scheduler so we don't
        iterate users who can't be reached anyway."""
        with cursor() as c:
            rows = c.execute("SELECT DISTINCT user_id FROM push_subscriptions").fetchall()
        return [r["user_id"] for r in rows]


# Note: due-aggregation queries (count_due_for_user, deck_due_breakdown)
# live on the study + decks contexts respectively — they're SRS-shaped
# data the scheduler needs but the queries belong with the data they
# count. See study.repo.ReviewRepo.count_due_for_user and
# decks.repo.DeckRepo.due_breakdown.


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
