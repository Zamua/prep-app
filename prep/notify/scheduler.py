"""Notification scheduler — periodic loop that decides when to fire.

Distinct from `prep.notify.push`, which owns the I/O (VAPID keys,
single-push send, fanout, subscription persistence). This module
holds the *policy*: which users, which mode (digest / when-ready),
which trivia decks, idempotency, quiet hours.

Tick model: wake every 5 minutes, walk the set of users who have
push subscriptions, evaluate each against their prefs. Idempotency
lives in the user's notification_prefs JSON (last_digest_date,
last_when_ready_at). Quiet hours skip both modes during the
configured night window.

After per-user evaluation, dispatches to the trivia-deck scheduler
(`prep.trivia.scheduler.tick`) which has its own per-deck cadence
+ exponential backoff logic.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from prep import db
from prep.notify.push import send_to_user

_log = logging.getLogger("prep.notify")

_TICK_SECONDS = 300  # 5 minutes
_WHEN_READY_DEBOUNCE_SECONDS = 4 * 60 * 60  # don't re-fire within 4h


def _in_quiet_hours(local_hour: int, quiet_start: int, quiet_end: int) -> bool:
    if quiet_start == quiet_end:
        return False
    if quiet_start < quiet_end:
        return quiet_start <= local_hour < quiet_end
    # Wraps midnight (e.g., 22..8): quiet from start..24 OR 0..end
    return local_hour >= quiet_start or local_hour < quiet_end


def _digest_body(deck_breakdown: list[tuple[str, int]], total: int) -> str:
    if total == 0:
        return ""
    if len(deck_breakdown) == 1:
        n, name = deck_breakdown[0][1], deck_breakdown[0][0]
        return f"{n} card{'s' if n != 1 else ''} due in {name}."
    head = ", ".join(f"{n} in {name}" for name, n in deck_breakdown[:3])
    extra = "" if len(deck_breakdown) <= 3 else f", + {len(deck_breakdown) - 3} more"
    return f"{total} cards due — {head}{extra}."


def _should_send_digest(prefs: dict, local_now: datetime) -> bool:
    """Digest: send once per local day at the configured hour. Allow a
    grace window so a 5-minute scheduler tick doesn't have to land exactly
    on the hour."""
    if prefs.get("mode") != "digest":
        return False
    target_hour = int(prefs.get("digest_hour", 9))
    # 5-min tick — if local hour matches, we're in the window. Don't
    # re-send if we already sent this local date.
    if local_now.hour != target_hour:
        return False
    today_iso = local_now.date().isoformat()
    return prefs.get("last_digest_date") != today_iso


def _should_send_when_ready(prefs: dict, due_total: int, now_utc: datetime) -> bool:
    if prefs.get("mode") != "when-ready":
        return False
    threshold = int(prefs.get("threshold", 3))
    if due_total < threshold:
        return False
    last = prefs.get("last_when_ready_at")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            if (now_utc - last_dt).total_seconds() < _WHEN_READY_DEBOUNCE_SECONDS:
                return False
        except ValueError:
            pass
    return True


async def _tick() -> None:
    """One scheduler iteration — evaluate every subscribed user and fire
    where appropriate."""
    now_utc = datetime.now(timezone.utc)
    for uid in db.list_users_with_push_subs():
        try:
            prefs = db.get_notification_prefs(uid)
            if prefs.get("mode") == "off":
                continue

            tz_name = prefs.get("tz", "America/New_York")
            try:
                tz = ZoneInfo(tz_name)
            except Exception:
                tz = ZoneInfo("America/New_York")
            local = now_utc.astimezone(tz)

            due_total = db.count_due_for_user(uid)
            if due_total == 0:
                continue

            mode = prefs.get("mode")
            if mode == "digest":
                # Digest fires at the user's deliberately-chosen hour — quiet
                # hours don't apply (the chosen hour IS the schedule). Idempotent
                # via last_digest_date so a 5-minute tick window won't double-fire.
                if _should_send_digest(prefs, local):
                    breakdown = db.deck_due_breakdown(uid)
                    send_to_user(
                        uid,
                        "Prep — daily digest",
                        _digest_body(breakdown, due_total),
                        url="/",
                        source="srs-digest",
                    )
                    prefs["last_digest_date"] = local.date().isoformat()
                    db.set_notification_prefs(uid, prefs)
            elif mode == "when-ready":
                # Quiet hours apply here — when-ready can fire any time and the
                # user might not want a 3am ping for a card that just rolled due.
                # Honor the opt-in: skip the check entirely if disabled.
                if prefs.get("quiet_hours_enabled") and _in_quiet_hours(
                    local.hour,
                    int(prefs.get("quiet_start_hour", 22)),
                    int(prefs.get("quiet_end_hour", 8)),
                ):
                    continue
                if _should_send_when_ready(prefs, due_total, now_utc):
                    send_to_user(
                        uid,
                        "Prep — cards ready",
                        f"{due_total} card{'s' if due_total != 1 else ''} due to study.",
                        url="/",
                        source="srs-when-ready",
                    )
                    prefs["last_when_ready_at"] = now_utc.isoformat()
                    db.set_notification_prefs(uid, prefs)
        except Exception as e:
            _log.exception("scheduler tick failed for user %s: %s", uid, e)

    # ---- trivia decks --------------------------------------------------
    # Distinct from the per-user srs digest/when-ready logic above. Each
    # trivia deck has its own per-deck interval (`notification_interval_
    # minutes`), so the dispatch logic lives in `prep.trivia.scheduler`
    # rather than inlined here. Late import keeps notify → trivia
    # one-way (trivia.scheduler imports send_to_user from us).
    try:
        from prep.trivia.scheduler import tick as _trivia_tick

        _trivia_tick(now_utc)
    except Exception as e:
        _log.exception("trivia scheduler tick threw: %s", e)


async def _scheduler_loop() -> None:
    while True:
        try:
            await _tick()
        except Exception as e:
            _log.exception("scheduler tick threw: %s", e)
        await asyncio.sleep(_TICK_SECONDS)


def start_scheduler() -> None:
    """Launch the background scheduler task on the running event loop.
    Call once from app startup. Idempotent — a second call is a no-op."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
    if getattr(start_scheduler, "_started", False):
        return
    loop.create_task(_scheduler_loop())
    start_scheduler._started = True
    _log.info("notification scheduler started (tick=%ss)", _TICK_SECONDS)
