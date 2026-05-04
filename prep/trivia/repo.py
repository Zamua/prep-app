"""Repository for the trivia bounded context.

Owns reads + writes against `trivia_queue` plus the trivia-deck-aware
joins against `questions`. The queue rules:

- New questions get inserted with `queue_position = max(queue_position)+1`
  for their deck (so newly-generated cards go to the back of the queue
  by default, and the scheduler hits never-answered cards in
  generation-order).
- `pick_next_for_deck` prefers never-answered cards (`last_answered_at
  IS NULL`) over rotated cards, then orders by ascending queue_position
  so the longest-since-shown card wins.
- `mark_answered` stamps `last_answered_at`/`last_answered_correctly`
  AND bumps `queue_position` to `max+1` so the just-answered card
  rotates to the back.
- `count_unanswered` powers the regen trigger — when it hits zero, the
  scheduler asks the agent for another batch before firing the next
  notification.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from prep.infrastructure.db import cursor
from prep.trivia.entities import (
    ActiveTriviaSession,
    NextCard,
    TriviaQueueEntry,
    TriviaSession,
)
from prep.trivia.session_state import format_done, parse_card_ids, parse_done


class TriviaQueueRepo:
    """SQL adapter for the trivia queue. Methods match the use cases
    in `prep.trivia.service` 1:1; nothing here knows about HTTP or
    Temporal."""

    def append_card(self, question_id: int, deck_id: int) -> TriviaQueueEntry:
        """Add a freshly-generated question to the back of its deck's
        queue. `queue_position` is allocated as `max+1` of all entries
        whose underlying question lives in the same deck — keeping the
        rotation order stable across batches.
        """
        with cursor() as c:
            row = c.execute(
                """
                SELECT COALESCE(MAX(tq.queue_position), 0) AS m
                FROM trivia_queue tq
                JOIN questions q ON q.id = tq.question_id
                WHERE q.deck_id = ?
                """,
                (deck_id,),
            ).fetchone()
            next_pos = (row["m"] or 0) + 1
            c.execute(
                """
                INSERT INTO trivia_queue (question_id, queue_position)
                VALUES (?, ?)
                """,
                (question_id, next_pos),
            )
            return TriviaQueueEntry(
                question_id=question_id,
                queue_position=next_pos,
            )

    def pick_next_for_deck(self, deck_id: int) -> Optional[NextCard]:
        """Pick the next card to notify on for `deck_id`.

        Weighted precedence (srs-lite for trivia):
          1. wrong-answered cards (`last_answered_correctly=0`),
             longest-since-shown first — these come back fast so the
             user gets a second chance while it still feels fresh
          2. never-answered cards (`last_answered_at IS NULL`),
             longest queued first — fresh content
          3. right-answered cards (`last_answered_correctly=1`),
             longest-since-shown first — review the well-known stuff
             after we've drained the more important categories

        The `is_fresh` flag preserves "never been shown" for the
        scheduler's gen-on-empty heuristic.
        """
        with cursor() as c:
            row = c.execute(
                """
                SELECT q.id AS question_id, q.deck_id, q.prompt,
                       (tq.last_answered_at IS NULL) AS is_fresh
                FROM questions q
                JOIN trivia_queue tq ON tq.question_id = q.id
                WHERE q.deck_id = ?
                ORDER BY
                  CASE
                    WHEN tq.last_answered_correctly = 0 THEN 0
                    WHEN tq.last_answered_at IS NULL    THEN 1
                    ELSE 2
                  END ASC,
                  tq.queue_position ASC
                LIMIT 1
                """,
                (deck_id,),
            ).fetchone()
        if row is None:
            return None
        return NextCard(
            question_id=row["question_id"],
            deck_id=row["deck_id"],
            prompt=row["prompt"],
            is_fresh=bool(row["is_fresh"]),
        )

    def mark_answered(self, question_id: int, correct: bool) -> None:
        """Record an answer + rotate the card to the back of its
        deck's queue. Bumping `queue_position` happens in the same
        UPDATE so the rotation is atomic w.r.t. the scheduler's
        next pick.
        """
        with cursor() as c:
            deck_id = self._deck_id_for_question(c, question_id)
            if deck_id is None:
                return
            new_pos_row = c.execute(
                """
                SELECT COALESCE(MAX(tq.queue_position), 0) + 1 AS np
                FROM trivia_queue tq
                JOIN questions q ON q.id = tq.question_id
                WHERE q.deck_id = ?
                """,
                (deck_id,),
            ).fetchone()
            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            c.execute(
                """
                UPDATE trivia_queue
                SET last_answered_at = ?,
                    last_answered_correctly = ?,
                    queue_position = ?
                WHERE question_id = ?
                """,
                (now_iso, 1 if correct else 0, new_pos_row["np"], question_id),
            )
            self._reset_deck_streak(c, deck_id)

    def set_last_correctness(self, question_id: int, correct: bool) -> None:
        """Flip the recorded verdict for a card without rotating it
        again. Used by the re-grade path: the card already moved when
        the user first answered, and re-grading shouldn't bump it to
        the back of the queue a second time. Also clears the deck's
        ignored-streak since a re-grade still counts as engagement."""
        with cursor() as c:
            deck_id = self._deck_id_for_question(c, question_id)
            if deck_id is None:
                return
            c.execute(
                """UPDATE trivia_queue
                   SET last_answered_correctly = ?
                   WHERE question_id = ?""",
                (1 if correct else 0, question_id),
            )
            self._reset_deck_streak(c, deck_id)

    @staticmethod
    def _deck_id_for_question(c, question_id: int) -> int | None:
        """Resolve the owning deck_id for a question. Returns None if
        the row is gone (caller should treat as a no-op)."""
        row = c.execute(
            "SELECT deck_id FROM questions WHERE id = ?",
            (question_id,),
        ).fetchone()
        return row["deck_id"] if row else None

    @staticmethod
    def _reset_deck_streak(c, deck_id: int) -> None:
        """Zero the deck's notification backoff. Both verdict-recording
        paths call this — any answer is engagement, so the next
        scheduler tick should fire at the deck's base interval rather
        than the backed-off one."""
        c.execute(
            "UPDATE decks SET notification_ignored_streak = 0 WHERE id = ?",
            (deck_id,),
        )

    def count_unanswered(self, deck_id: int) -> int:
        """Number of cards in `deck_id` whose `last_answered_at IS NULL`.
        Strict "never seen" count — used by tests pinning queue
        rotation behavior."""
        with cursor() as c:
            row = c.execute(
                """
                SELECT COUNT(*) AS n
                FROM trivia_queue tq
                JOIN questions q ON q.id = tq.question_id
                WHERE q.deck_id = ? AND tq.last_answered_at IS NULL
                """,
                (deck_id,),
            ).fetchone()
        return int(row["n"] or 0)

    def deck_stats(self, deck_id: int) -> dict:
        """Aggregate counts for the deck-page mastery bar:
        {total, unanswered, wrong, mastered}. One query, group by
        the three queue states."""
        with cursor() as c:
            row = c.execute(
                """
                SELECT
                  COUNT(*) AS total,
                  SUM(CASE WHEN tq.last_answered_at IS NULL THEN 1 ELSE 0 END) AS unanswered,
                  SUM(CASE WHEN tq.last_answered_correctly = 0 THEN 1 ELSE 0 END) AS wrong,
                  SUM(CASE WHEN tq.last_answered_correctly = 1 THEN 1 ELSE 0 END) AS mastered
                FROM trivia_queue tq
                JOIN questions q ON q.id = tq.question_id
                WHERE q.deck_id = ?
                """,
                (deck_id,),
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "unanswered": int(row["unanswered"] or 0),
            "wrong": int(row["wrong"] or 0),
            "mastered": int(row["mastered"] or 0),
        }

    def has_answer_since(self, deck_id: int, ts: str | None) -> bool:
        """True if any card in `deck_id` has been answered after `ts`.
        Drives the scheduler's exponential-backoff engagement check —
        if the user has touched any card in the deck since the last
        push fired, we treat the prior push as engaged-with and reset
        the ignored streak. None ts is treated as "no prior fire" → no
        engagement to credit yet (scheduler treats this case separately
        anyway)."""
        if ts is None:
            return False
        with cursor() as c:
            row = c.execute(
                """
                SELECT 1
                FROM trivia_queue tq
                JOIN questions q ON q.id = tq.question_id
                WHERE q.deck_id = ?
                  AND tq.last_answered_at IS NOT NULL
                  AND tq.last_answered_at > ?
                LIMIT 1
                """,
                (deck_id, ts),
            ).fetchone()
        return row is not None

    def count_pending_review(self, deck_id: int) -> int:
        """Cards that still need work — never-shown OR previously
        wrong. Drives the scheduler's "should I generate more?"
        decision: don't bloat the deck with fresh content while the
        user still has unanswered/wrong cards to grind through."""
        with cursor() as c:
            row = c.execute(
                """
                SELECT COUNT(*) AS n
                FROM trivia_queue tq
                JOIN questions q ON q.id = tq.question_id
                WHERE q.deck_id = ?
                  AND (tq.last_answered_at IS NULL
                       OR tq.last_answered_correctly = 0)
                """,
                (deck_id,),
            ).fetchone()
        return int(row["n"] or 0)

    def pick_session_for_deck(
        self, deck_id: int, *, target_size: int = 3, fresh_target: int = 1
    ) -> list[NextCard]:
        """Pick up to `target_size` cards for a notification mini-session.

        Aims for a mix: `fresh_target` never-shown cards (default 1)
        plus `target_size - fresh_target` review cards (default 2).
        Within the review slot, wrong-answered cards outrank
        correctly-answered ones (matches the single-card weighted
        precedence). Order returned: review first, fresh last — the
        idea is to clear accumulated debt before being rewarded with
        fresh content.

        Backfill: if either pool comes up short, the other fills the
        gap. A brand-new deck (no review yet) becomes 3 fresh; a deck
        with no fresh left becomes 3 review. Never returns more than
        `target_size`; may return zero if the deck is empty.
        """
        review_slots = target_size - fresh_target
        with cursor() as c:
            review_rows = (
                c.execute(
                    """
                    SELECT q.id AS question_id, q.deck_id, q.prompt, 0 AS is_fresh
                    FROM questions q
                    JOIN trivia_queue tq ON tq.question_id = q.id
                    WHERE q.deck_id = ? AND tq.last_answered_at IS NOT NULL
                    ORDER BY
                      CASE WHEN tq.last_answered_correctly = 0 THEN 0 ELSE 1 END,
                      tq.queue_position
                    LIMIT ?
                    """,
                    (deck_id, review_slots),
                ).fetchall()
                if review_slots > 0
                else []
            )
            fresh_rows = (
                c.execute(
                    """
                    SELECT q.id AS question_id, q.deck_id, q.prompt, 1 AS is_fresh
                    FROM questions q
                    JOIN trivia_queue tq ON tq.question_id = q.id
                    WHERE q.deck_id = ? AND tq.last_answered_at IS NULL
                    ORDER BY tq.queue_position
                    LIMIT ?
                    """,
                    (deck_id, fresh_target),
                ).fetchall()
                if fresh_target > 0
                else []
            )
            picked_ids = {r["question_id"] for r in review_rows} | {
                r["question_id"] for r in fresh_rows
            }
            short = target_size - len(picked_ids)
            backfill_rows = []
            if short > 0:
                placeholders = ",".join("?" * len(picked_ids)) if picked_ids else "NULL"
                backfill_rows = c.execute(
                    f"""
                    SELECT q.id AS question_id, q.deck_id, q.prompt,
                           (tq.last_answered_at IS NULL) AS is_fresh
                    FROM questions q
                    JOIN trivia_queue tq ON tq.question_id = q.id
                    WHERE q.deck_id = ? AND q.id NOT IN ({placeholders})
                    ORDER BY
                      CASE
                        WHEN tq.last_answered_correctly = 0 THEN 0
                        WHEN tq.last_answered_at IS NULL    THEN 1
                        ELSE 2
                      END,
                      tq.queue_position
                    LIMIT ?
                    """,
                    (deck_id, *picked_ids, short),
                ).fetchall()
        ordered = list(review_rows) + list(fresh_rows) + list(backfill_rows)
        return [
            NextCard(
                question_id=r["question_id"],
                deck_id=r["deck_id"],
                prompt=r["prompt"],
                is_fresh=bool(r["is_fresh"]),
            )
            for r in ordered[:target_size]
        ]

    def prompt_for_question(self, question_id: int) -> str | None:
        """Cheap fetch of just the prompt text for a single qid.
        Used by the scheduler when resuming a session — only the
        head card's prompt is needed for the notification body."""
        with cursor() as c:
            row = c.execute(
                "SELECT prompt FROM questions WHERE id = ?",
                (question_id,),
            ).fetchone()
        return row["prompt"] if row else None

    def existing_prompts(self, deck_id: int) -> list[str]:
        """All current question prompts for `deck_id`. Passed to the
        generator so the next batch doesn't repeat anything we've
        already created.
        """
        with cursor() as c:
            rows = c.execute(
                "SELECT prompt FROM questions WHERE deck_id = ? ORDER BY id",
                (deck_id,),
            ).fetchall()
        return [r["prompt"] for r in rows]


# Idle threshold after which an "active" session is auto-marked
# abandoned. Mirrors the SRS reaper window (study_sessions also uses
# 7 days). Applied lazily on `list_active`.
_ACTIVE_TIMEOUT = timedelta(days=7)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TriviaSessionsRepo:
    """Persistence for the URL-encoded mini-sessions.

    Invariant: at most ONE row per (user_id, deck_id) with
    status='active'. The `start_or_resume` and `replace_active`
    methods enforce this — there's no unique index because completed
    / abandoned rows for the same (user, deck) are common.

    `queue` and `done` mirror the URL params (`?cards=…&done=…`).
    Stored as the same TEXT format so persistence is lossless and
    the route can swap between URL state and DB state freely.
    """

    def get_active_for_deck(self, user_id: str, deck_id: int) -> TriviaSession | None:
        """Return the active session for (user, deck) if any. None if
        none active. Doesn't run the abandon reaper — callers that
        need fresh state should hit `list_active` first."""
        with cursor() as c:
            row = c.execute(
                "SELECT * FROM trivia_sessions"
                " WHERE user_id = ? AND deck_id = ? AND status = 'active'"
                " ORDER BY last_active DESC LIMIT 1",
                (user_id, deck_id),
            ).fetchone()
        return _row_to_trivia_session(row) if row else None

    def list_active(self, user_id: str) -> list[ActiveTriviaSession]:
        """All active trivia sessions for the user, joined with deck
        names for the index "Continue" strip. Side-effect: idle >7d
        sessions get aged to status='abandoned' (cheap inline reaper,
        same pattern as SessionRepo.list_recent)."""
        threshold = (datetime.now(timezone.utc) - _ACTIVE_TIMEOUT).isoformat(timespec="seconds")
        with cursor() as c:
            c.execute(
                "UPDATE trivia_sessions SET status = 'abandoned'"
                " WHERE user_id = ? AND status = 'active' AND last_active < ?",
                (user_id, threshold),
            )
            rows = c.execute(
                """
                SELECT s.deck_id, s.last_active, s.queue, s.done, d.name AS deck_name
                  FROM trivia_sessions s
                  JOIN decks d ON d.id = s.deck_id
                 WHERE s.user_id = ? AND s.status = 'active'
                 ORDER BY s.last_active DESC
                """,
                (user_id,),
            ).fetchall()
        return [
            ActiveTriviaSession(
                deck_name=r["deck_name"],
                deck_id=r["deck_id"],
                last_active=r["last_active"],
                queue=parse_card_ids(r["queue"]),
                done=parse_done(r["done"]),
            )
            for r in rows
        ]

    def start_or_resume(
        self, user_id: str, deck_id: int, *, queue: list[int], done: list[tuple[int, str]]
    ) -> TriviaSession:
        """Insert a new active session OR refresh `last_active` on an
        existing one for (user, deck). Returns the row either way.

        If an active row exists, its persisted queue + done are
        preserved (the URL state is treated as a navigation token,
        not the source of truth — important for resuming from a
        stale notification log entry). If you want to FORCE a fresh
        queue (e.g., scheduler firing an explicit new pick), use
        `replace_active` instead.
        """
        existing = self.get_active_for_deck(user_id, deck_id)
        now = _now_iso()
        if existing:
            with cursor() as c:
                c.execute(
                    "UPDATE trivia_sessions SET last_active = ? WHERE id = ?",
                    (now, existing.id),
                )
            existing.last_active = now
            return existing
        sid = uuid.uuid4().hex[:16]
        with cursor() as c:
            c.execute(
                "INSERT INTO trivia_sessions"
                " (id, user_id, deck_id, started_at, last_active, status, queue, done)"
                " VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
                (
                    sid,
                    user_id,
                    deck_id,
                    now,
                    now,
                    ",".join(str(q) for q in queue),
                    format_done(done),
                ),
            )
        return TriviaSession(
            id=sid,
            user_id=user_id,
            deck_id=deck_id,
            started_at=now,
            last_active=now,
            status="active",
            queue=list(queue),
            done=list(done),
        )

    def replace_active(self, user_id: str, deck_id: int, *, queue: list[int]) -> TriviaSession:
        """Abandon any existing active session for (user, deck) and
        start a fresh one with the given queue. Used by the
        scheduler when no active session exists OR when the existing
        one is empty (just-completed) — the scheduler picks a new
        queue and we drop the old row."""
        with cursor() as c:
            c.execute(
                "UPDATE trivia_sessions SET status = 'abandoned'"
                " WHERE user_id = ? AND deck_id = ? AND status = 'active'",
                (user_id, deck_id),
            )
        return self.start_or_resume(user_id, deck_id, queue=queue, done=[])

    def persist_state(
        self, user_id: str, deck_id: int, *, queue: list[int], done: list[tuple[int, str]]
    ) -> None:
        """Update the active session's queue + done after an answer.
        No-op if no active session exists (caller is mid-flow without
        a persistence row, fine — caller can call start_or_resume
        next time)."""
        with cursor() as c:
            c.execute(
                "UPDATE trivia_sessions SET queue = ?, done = ?, last_active = ?"
                " WHERE user_id = ? AND deck_id = ? AND status = 'active'",
                (
                    ",".join(str(q) for q in queue),
                    format_done(done),
                    _now_iso(),
                    user_id,
                    deck_id,
                ),
            )

    def complete(self, user_id: str, deck_id: int) -> None:
        """Mark the active session for (user, deck) as completed.
        Called when the user reaches the empty-queue summary view."""
        with cursor() as c:
            c.execute(
                "UPDATE trivia_sessions SET status = 'completed', last_active = ?"
                " WHERE user_id = ? AND deck_id = ? AND status = 'active'",
                (_now_iso(), user_id, deck_id),
            )


def _row_to_trivia_session(row) -> TriviaSession:
    return TriviaSession(
        id=row["id"],
        user_id=row["user_id"],
        deck_id=row["deck_id"],
        started_at=row["started_at"],
        last_active=row["last_active"],
        status=row["status"],
        queue=parse_card_ids(row["queue"]),
        done=parse_done(row["done"]),
    )
