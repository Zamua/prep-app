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
        When this hits zero AND the deck still has questions, the
        scheduler should fire a fresh-batch generation before its next
        tick (or ride out the rotation, depending on policy)."""
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
