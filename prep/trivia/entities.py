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


class TriviaSession(BaseModel):
    """Persistent record of a trivia mini-session in progress.

    The URL-encoded `?cards=…&done=…` form is the canonical
    interactive state during a session — refresh / back-button work
    because the URL holds everything. This row is the RECOVERY
    cache: it lets the user resume a session after closing the tab,
    crashing, or switching devices, and powers the "Continue" strip
    on the index page + the resume-aware notification body.

    Invariant (enforced by the repo): at most ONE row per
    (user_id, deck_id) with status='active'. Completed / abandoned
    rows for the same pair are fine.
    """

    id: str
    user_id: str
    deck_id: int
    started_at: str
    last_active: str
    status: str  # active | completed | abandoned
    queue: list[int]
    done: list[tuple[int, str]]


class ActiveTriviaSession(BaseModel):
    """Index-page CTA shape: an active session joined with the
    deck's name so the rendering template doesn't need a second
    query per row. Returned by `TriviaSessionsRepo.list_active`.
    """

    deck_name: str
    deck_id: int
    last_active: str
    queue: list[int]
    done: list[tuple[int, str]]

    @property
    def remaining(self) -> int:
        return len(self.queue)

    @property
    def total(self) -> int:
        return len(self.queue) + len(self.done)
