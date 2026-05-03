"""Scheduler dispatch for trivia decks.

Called once per scheduler tick from `prep.notify._legacy_module._tick`.
Walks all trivia decks, picks the ones whose interval has elapsed,
fires one web push per ready deck, and (if the deck's queue is empty
or has run out of unanswered cards) generates a fresh batch via
`prep.trivia.service.generate_batch` first.

The dispatch logic lives here (in the trivia context) rather than
inside notify/_legacy_module so that the per-deck rules — interval
arithmetic, queue inspection, batch regen on empty — stay close to
the trivia bounded context that owns them. notify/_legacy_module just
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

# When the count of "still needs work" cards (never-shown + wrong)
# drops below this, the scheduler proactively asks claude for a
# fresh batch on its next tick. Above the threshold, no generation
# fires — the deck doesn't bloat while the user still has material
# to grind through.
_REFILL_BELOW_PENDING_REVIEW = 5


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


def _is_due(now_utc: datetime, last_notified_at: Optional[str], interval_minutes: int) -> bool:
    """A trivia deck is due if it's never been notified, OR if at
    least `interval_minutes` have passed since `last_notified_at`."""
    if not last_notified_at:
        return True
    last_dt = _parse_iso(last_notified_at)
    if last_dt is None:
        return True
    return (now_utc - last_dt).total_seconds() >= interval_minutes * 60


def tick(now_utc: datetime) -> None:
    """One scheduler iteration's worth of trivia work. Idempotent;
    safe to call from the existing `_tick` loop. Send_to_user is
    pulled in via late import so this module doesn't grow a cycle
    against notify/_legacy_module (which imports us)."""
    from prep.notify._legacy_module import send_to_user

    decks = DeckRepo()
    questions = QuestionRepo()
    trivia = TriviaQueueRepo()

    for row in decks.list_trivia_decks():
        try:
            deck_id = row["id"]
            # Per-deck mute switch. Skip silently — toggling notifications
            # off should be cheap and not leave half-notified state behind.
            if not row.get("notifications_enabled", 1):
                continue
            interval = row.get("notification_interval_minutes") or _DEFAULT_INTERVAL_MINUTES
            if not _is_due(now_utc, row.get("last_notified_at"), interval):
                continue

            # Refill gate: if the user is running low on cards that
            # still need work (never-shown + wrong-answered), ask
            # claude for a fresh batch BEFORE picking. Synchronous —
            # the scheduler tick happily blocks ~30-60s; next tick
            # is _TICK_SECONDS away regardless. Failures are logged
            # and swallowed: an unavailable agent shouldn't stop us
            # from cycling existing cards (the weighted picker can
            # still surface review material).
            if trivia.count_pending_review(deck_id) < _REFILL_BELOW_PENDING_REVIEW:
                topic = (row.get("context_prompt") or row.get("name") or "").strip()
                if topic:
                    try:
                        trivia_service.generate_batch(
                            user_id=row["user_id"],
                            deck_id=deck_id,
                            topic=topic,
                            questions_repo=questions,
                            trivia_repo=trivia,
                        )
                    except AgentUnavailable as e:
                        logger.warning("trivia tick: refill failed for deck %s: %s", deck_id, e)

            nxt = trivia.pick_next_for_deck(deck_id)
            if nxt is None:
                # Deck has zero cards — refill must have failed and
                # there's nothing left to recycle. Bail this round.
                logger.warning("trivia tick: deck %s has no cards; skipping", deck_id)
                continue

            # Fire the push. Body = question text (trimmed for native
            # platform limits — most platforms cap around 120 chars
            # and gracefully truncate, but we be polite).
            body = nxt.prompt
            if len(body) > 240:
                body = body[:237] + "..."
            send_to_user(
                user_id=row["user_id"],
                title=row.get("name") or "Trivia",
                body=body,
                url=f"/trivia/{nxt.question_id}",
            )

            decks.set_last_notified_at(deck_id, now_utc.isoformat(timespec="seconds"))

        except Exception as e:
            # Per-deck try block: a malformed row shouldn't tank the
            # whole scheduler tick. Log loudly and move on.
            logger.exception("trivia tick failed for deck %s: %s", row.get("id"), e)
