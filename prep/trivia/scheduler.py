"""Scheduler dispatch for trivia decks.

Called once per scheduler tick from `prep.notify.scheduler._tick`.
Walks all trivia decks, picks the ones whose interval has elapsed,
fires one web push per ready deck, and (if the deck's queue is empty
or has run out of unanswered cards) generates a fresh batch via
`prep.trivia.service.generate_batch` first.

The dispatch logic lives here (in the trivia context) rather than
inside notify/scheduler so that the per-deck rules — interval
arithmetic, queue inspection, batch regen on empty — stay close to
the trivia bounded context that owns them. notify/scheduler just
calls `tick()` once per its own loop iteration.

Failure mode: anything that raises inside this module is logged and
swallowed at the deck-by-deck boundary. A flaky agent or a malformed
deck row should never tank the whole scheduler.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from prep.decks.repo import DeckRepo, QuestionRepo
from prep.trivia import service as trivia_service
from prep.trivia.agent_client import AgentUnavailable
from prep.trivia.repo import TriviaQueueRepo

logger = logging.getLogger(__name__)


_DEFAULT_INTERVAL_MINUTES = 30

# Strict gate: only refill the deck once the user has answered EVERY
# existing card correctly at least once. This stops the scheduler
# from blowing the deck up to hundreds of cards when the user has
# stopped engaging — if there's a single wrong/never-shown card
# left, no new generation fires.
_REFILL_BELOW_PENDING_REVIEW = 1

# Exponential backoff cap. Effective interval is
# `base × 2 ** min(streak, _MAX_BACKOFF_DOUBLINGS)`, so 5 doublings =
# 32× cap. With a 30-minute deck that's 16 hours between pushes once
# the user has fully checked out — quiet enough to stop being
# annoying but still occasionally surfaces the deck so a returning
# user gets re-engaged automatically.
_MAX_BACKOFF_DOUBLINGS = 5


def _parse_iso(ts: str) -> Optional[datetime]:
    """Tolerant ISO-8601 parse for the deck's last_notified_at column.
    Returns None on garbage so the caller treats the deck as
    never-notified (and fires immediately)."""
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _effective_interval_minutes(base_minutes: int, ignored_streak: int) -> int:
    """Apply exponential backoff: `base × 2 ** min(streak, MAX)`. A
    streak of 0 (never ignored, or just engaged) returns base; each
    consecutive ignored fire doubles, capped at MAX doublings."""
    capped = max(0, min(ignored_streak, _MAX_BACKOFF_DOUBLINGS))
    return base_minutes * (2**capped)


def _is_due(
    now_utc: datetime,
    last_notified_at: Optional[str],
    interval_minutes: int,
    ignored_streak: int = 0,
) -> bool:
    """A trivia deck is due if it's never been notified, OR if at
    least the (backed-off) effective interval has passed since
    `last_notified_at`."""
    if not last_notified_at:
        return True
    last_dt = _parse_iso(last_notified_at)
    if last_dt is None:
        return True
    effective = _effective_interval_minutes(interval_minutes, ignored_streak)
    return (now_utc - last_dt).total_seconds() >= effective * 60


def tick(now_utc: datetime) -> None:
    """One scheduler iteration's worth of trivia work. Idempotent;
    safe to call from the existing `_tick` loop. Send_to_user is
    pulled in via late import so this module doesn't grow a cycle
    against prep.notify.scheduler (which imports us)."""
    from prep.notify.push import send_to_user

    decks = DeckRepo()
    questions = QuestionRepo()
    trivia = TriviaQueueRepo()

    # Per-user prefs lookup; cached for this tick so we don't re-read
    # for every deck owned by the same user. Quiet hours apply to
    # trivia notifications now too (decoupled from SRS when-ready).
    from prep.auth.repo import UserRepo

    users = UserRepo()
    prefs_cache: dict[str, dict] = {}

    def _user_in_quiet_hours(user_id: str) -> bool:
        prefs = prefs_cache.get(user_id)
        if prefs is None:
            try:
                prefs = users.get_notification_prefs(user_id)
            except Exception:
                prefs = {}
            prefs_cache[user_id] = prefs
        if not prefs.get("quiet_hours_enabled"):
            return False
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(prefs.get("tz") or "America/New_York")
        except Exception:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo("America/New_York")
        local_hour = now_utc.astimezone(tz).hour
        start = int(prefs.get("quiet_start_hour", 22))
        end = int(prefs.get("quiet_end_hour", 8))
        if start == end:
            return False
        if start < end:
            return start <= local_hour < end
        # Wraps midnight (22..8).
        return local_hour >= start or local_hour < end

    for deck in decks.list_trivia_decks():
        try:
            # Per-deck mute switch. Skip silently — toggling notifications
            # off should be cheap and not leave half-notified state behind.
            if not deck.notifications_enabled:
                continue
            interval = deck.notification_interval_minutes or _DEFAULT_INTERVAL_MINUTES
            streak = deck.notification_ignored_streak
            if not _is_due(now_utc, deck.last_notified_at, interval, streak):
                continue
            # Quiet hours apply across SRS when-ready + trivia. Skip
            # without touching last_notified_at so the deck fires as
            # soon as the window reopens.
            if _user_in_quiet_hours(deck.user_id):
                continue

            # Refill gate: if the user is running low on cards that
            # still need work (never-shown + wrong-answered), ask
            # claude for a fresh batch BEFORE picking. Synchronous —
            # the scheduler tick happily blocks ~30-60s; next tick
            # is _TICK_SECONDS away regardless. Failures are logged
            # and swallowed: an unavailable agent shouldn't stop us
            # from cycling existing cards (the weighted picker can
            # still surface review material).
            if trivia.count_pending_review(deck.id) < _REFILL_BELOW_PENDING_REVIEW:
                topic = (deck.context_prompt or deck.name or "").strip()
                if topic:
                    try:
                        trivia_service.generate_batch(
                            user_id=deck.user_id,
                            deck_id=deck.id,
                            topic=topic,
                            questions_repo=questions,
                            trivia_repo=trivia,
                        )
                    except AgentUnavailable as e:
                        logger.warning("trivia tick: refill failed for deck %s: %s", deck.id, e)

            # Pick the FULL session here, not just the head card —
            # this way the notification body matches the first card
            # the user will see when they tap, and we can encode the
            # whole queue in the deep-link URL so the route doesn't
            # re-pick (which previously caused divergence: the body
            # showed a different card than the one that opened).
            target_size = deck.trivia_session_size or 3
            fresh_target = max(1, target_size // 2)
            session_cards = trivia.pick_session_for_deck(
                deck.id, target_size=target_size, fresh_target=fresh_target
            )
            if not session_cards:
                # Deck has zero cards — refill must have failed and
                # there's nothing left to recycle. Bail this round.
                logger.warning("trivia tick: deck %s has no cards; skipping", deck.id)
                continue

            # Fire the push. Body = first card's prompt — same card
            # the route will render when the user taps. Trimmed for
            # native platform limits (most cap ~120 chars and
            # gracefully truncate, but we be polite).
            head = session_cards[0]
            body = head.prompt
            if len(body) > 240:
                body = body[:237] + "..."
            # Encode the whole picked queue in the URL so the session
            # route renders this exact session instead of re-picking
            # at tap time. Order preserved: the user sees `head` first.
            cards_param = ",".join(str(c.question_id) for c in session_cards)
            # Engagement check before fire: if the user has answered
            # any card in this deck since the last push went out, the
            # prior fire counts as engaged-with → reset the streak.
            # Otherwise the prior fire was ignored → bump the streak
            # (capped at MAX). We update streak BEFORE firing so the
            # newly-recorded value reflects "this push is the one that
            # went out at the backed-off cadence".
            engaged = trivia.has_answer_since(deck.id, deck.last_notified_at)
            new_streak = 0 if engaged else min(streak + 1, _MAX_BACKOFF_DOUBLINGS)

            # Per-deck tag so iOS coalesces stacked notifications —
            # a new push for the same deck replaces the prior one
            # rather than piling up under the app icon. Without this
            # the tag falls back to "prep-default" (set in sw.js),
            # which is shared across decks/sources and doesn't dedupe.
            send_to_user(
                user_id=deck.user_id,
                title=deck.name or "Trivia",
                body=body,
                url=f"/trivia/session/{deck.name}?cards={cards_param}",
                source="trivia",
                tag=f"trivia-{deck.name}" if deck.name else "trivia",
            )

            decks.record_notification_fire(
                deck.id, now_utc.isoformat(timespec="seconds"), new_streak
            )

        except Exception as e:
            # Per-deck try block: a malformed row shouldn't tank the
            # whole scheduler tick. Log loudly and move on.
            logger.exception("trivia tick failed for deck %s: %s", deck.id, e)
