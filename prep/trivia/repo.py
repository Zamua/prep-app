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

from datetime import datetime, timezone
from typing import Optional

from prep.infrastructure.db import cursor
from prep.trivia.entities import NextCard, TriviaQueueEntry


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
        deck_id_row = None
        with cursor() as c:
            deck_id_row = c.execute(
                "SELECT deck_id FROM questions WHERE id = ?",
                (question_id,),
            ).fetchone()
            if deck_id_row is None:
                return
            deck_id = deck_id_row["deck_id"]
            new_pos_row = c.execute(
                """
                SELECT COALESCE(MAX(tq.queue_position), 0) + 1 AS np
                FROM trivia_queue tq
                JOIN questions q ON q.id = tq.question_id
                WHERE q.deck_id = ?
                """,
                (deck_id,),
            ).fetchone()
            new_pos = new_pos_row["np"]
            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            c.execute(
                """
                UPDATE trivia_queue
                SET last_answered_at = ?,
                    last_answered_correctly = ?,
                    queue_position = ?
                WHERE question_id = ?
                """,
                (now_iso, 1 if correct else 0, new_pos, question_id),
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
