"""Entities for the offline bounded context.

Typed views over the snapshot payload served by
GET /api/offline/snapshot and the sync batch exchanged over
POST /api/offline/sync (docs/OFFLINE.md section 4). Same discipline
as the other contexts: repos return these entities; the route
serializes them at the boundary.

The sync REQUEST item models are deliberately permissive: semantic
validation (verdict vocabulary, timestamp shape, target resolution,
prompt/answer presence) happens per item in the service so one bad
item lands in "rejected" instead of 4xx-ing the whole batch. Only
the batch-level caps are enforced at the pydantic layer -- an
over-cap request is a protocol violation by the client (which chunks
under the caps), not a bad item.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# Batch caps (docs/OFFLINE.md section 4). The client chunks under
# these; the server rejects anything larger at the parse layer.
INBOX_DECK_NAME = "inbox"

MAX_SYNC_CARDS = 100
MAX_SYNC_REVIEWS = 500


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


# ---- sync request (client -> server) -----------------------------------


class SyncNewCard(BaseModel):
    """One offline-authored card queued for ingestion. `deck_id` None
    files the card into the get-or-created `inbox` deck. `created_at`
    is client bookkeeping; the server stamps its own creation time.

    Fields are typed Any on purpose: a wrong-typed value (a corrupt
    outbox row) must reject THAT item in the service, not 422 the
    batch at the parse layer, or one bad row wedges the whole outbox
    forever."""

    client_id: Any = None
    deck_id: Any = None
    prompt: Any = ""
    answer: Any = ""
    created_at: Any = None


class SyncReview(BaseModel):
    """One queued offline review. Exactly one of `question_id` (a
    snapshot card) or `card_client_id` (an offline-authored card's
    client UUID) identifies the target. `graded_by` is 'auto'
    (deterministic offline grader) or 'self' (reveal + self-verdict);
    `reviewed_at` must be ISO-8601 WITH a UTC offset.

    Same Any-typing rationale as SyncNewCard: semantic validation is
    per item in the service, so type garbage rejects the item instead
    of poisoning the batch."""

    client_id: Any = None
    question_id: Any = None
    card_client_id: Any = None
    verdict: Any = ""
    user_answer: Any = ""
    graded_by: Any = ""
    reviewed_at: Any = ""


class SyncRequest(BaseModel):
    """The POST /api/offline/sync body: cards then reviews, capped.
    Items must be JSON objects and the caps must hold (both are
    protocol violations by a client that always chunks under them);
    everything inside an item is validated per item."""

    device_id: Any = None
    new_cards: list[SyncNewCard] = Field(default_factory=list, max_length=MAX_SYNC_CARDS)
    reviews: list[SyncReview] = Field(default_factory=list, max_length=MAX_SYNC_REVIEWS)


# ---- sync response (server -> client) ----------------------------------


class SyncCardResult(BaseModel):
    """Per-card outcome: 'created' (with the server question_id the
    client maps its client_id to) or 'rejected' (with the error)."""

    client_id: str | None
    status: str
    question_id: int | None = None
    error: str | None = None


class SyncReviewResult(BaseModel):
    """Per-review outcome: 'applied' (ran the scheduler),
    'logged_no_reschedule' (audit row only; a later review already
    owned the card state), or 'rejected' (with the error)."""

    client_id: str | None
    status: str
    error: str | None = None


class SyncResponse(BaseModel):
    """Item-by-item fates, in request order, keyed by client_id."""

    cards: list[SyncCardResult]
    reviews: list[SyncReviewResult]
