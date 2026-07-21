"""Entities for the offline bounded context.

Typed views over the snapshot payload served by
GET /api/offline/snapshot. Same discipline as the other contexts:
repos return these entities; the route serializes them at the
boundary.
"""

from __future__ import annotations

from pydantic import BaseModel


class SnapshotDeck(BaseModel):
    """A deck as the offline app needs it: enough to render the deck
    picker and group cards. SRS decks only; the offline surface does
    not cover trivia (docs/OFFLINE.md non-goals)."""

    id: int
    name: str
    display_name: str | None = None


class SnapshotCard(BaseModel):
    """A studiable card in the snapshot: the question fields the
    offline grader + reveal flow need, plus the coarse SRS view.

    `step` is the 0-5 maturity bucket derived from FSRS stability
    (prep.domain.srs.step_for_stability); it doubles as the seed for
    the client's local ladder. `next_due` is the server-computed due
    timestamp. `choices` is decoded to a list at the repo boundary;
    `answer` stays the raw stored string (a JSON array for `multi`),
    matching the online entities."""

    question_id: int
    deck_id: int
    type: str
    prompt: str
    choices: list[str] | None = None
    answer: str
    answer_regex: str | None = None
    rubric: str | None = None
    skeleton: str | None = None
    explanation: str | None = None
    step: int
    next_due: str
