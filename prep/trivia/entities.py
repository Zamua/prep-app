"""Entities for the trivia bounded context.

`TriviaQueueEntry` is the per-question queue/answer state. It mirrors
the `trivia_queue` table 1:1 — same shape philosophy as decks/entities.

`NextCard` is the small DTO routes/services use when picking what to
notify about. It deliberately keeps just enough info for the push body
+ deep link, not the whole question payload (the route loads that
separately on tap).
"""

from __future__ import annotations

from pydantic import BaseModel


class TriviaQueueEntry(BaseModel):
    """Per-question state inside a trivia deck's rotation.

    `queue_position` is monotonically increasing within the deck.
    Lower values = "next to fire". After answering, the entry's
    queue_position is bumped to `max(queue_position)+1`, rotating it
    to the back of the queue. `last_answered_at IS NULL` means the
    card has never been served; the picker prefers those over
    rotated-already cards.
    """

    question_id: int
    queue_position: int
    last_answered_at: str | None = None
    last_answered_correctly: bool | None = None


class NextCard(BaseModel):
    """The minimum info the scheduler needs to fire one notification.

    Returned by `TriviaQueueRepo.pick_next_for_deck`.
    """

    question_id: int
    deck_id: int
    prompt: str
    # True iff this is a never-answered card (preferred over rotated).
    is_fresh: bool
