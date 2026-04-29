"""Entities for the decks bounded context.

`Deck` and `Question` are aggregate roots for this context. Other
contexts (study, agent) hold references to them by ID, never by
mutating them directly — they go through services that take/return
entities.

The fields mirror the current sqlite columns 1:1 — the entity is a
typed view over the row dict, not a remodeling of the schema. That
keeps the migration cheap (repos just `Question.model_validate(dict(row))`)
while still giving us validation at the boundary.

Where the wire format differs from the typed view:
- `choices` stored as a JSON string in sqlite, exposed as `list[str]`
  on the entity. The repo handles the encode/decode.
- `answer` stored as a TEXT column. For `multi` questions it's a JSON
  array; for everything else it's a plain string. The entity preserves
  the current behavior — answer is a `str` either way; callers that
  need the list-form decode it themselves. (Phase 6 will revisit this
  when we extract the study context's grader path.)
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class QuestionType(str, Enum):
    """The four question types the app supports.

    Code + short are LLM-graded (Temporal worker); mcq + multi are
    deterministic (prep.domain.grading)."""

    CODE = "code"
    MCQ = "mcq"
    MULTI = "multi"
    SHORT = "short"


class Deck(BaseModel):
    """A deck — named container for questions, scoped to a user."""

    id: int
    user_id: str
    name: str = Field(min_length=1, max_length=200)
    created_at: str
    context_prompt: str | None = None


class DeckSummary(BaseModel):
    """The shape returned by `list_decks` for the index page — adds
    aggregate counts (`total`, `due`) without dragging in the full
    questions list. Distinct type so the typing tells you which view
    you're holding.
    """

    id: int
    name: str
    total: int
    due: int


class Question(BaseModel):
    """A flashcard — prompt + type-specific answer key + SRS state."""

    id: int
    user_id: str
    deck_id: int
    type: QuestionType
    topic: str | None = None
    prompt: str = Field(min_length=1)
    # Stored as a JSON string in sqlite; repos pre-decode to list[str].
    # None for non-mcq/multi types.
    choices: list[str] | None = None
    # `answer` shape depends on type:
    #   - mcq:    the chosen value (str)
    #   - multi:  a JSON array of values (str — caller decodes if needed)
    #   - code:   the canonical solution / reference answer (str)
    #   - short:  the canonical short answer (str)
    answer: str
    rubric: str | None = None
    created_at: str
    suspended: bool = False
    # `code`-only fields. None for everything else; the repo enforces
    # this at write time.
    skeleton: str | None = None
    language: str | None = None


class QuestionWithSrsState(Question):
    """A question joined with its SRS state. Returned by routes that
    need to render due-status (the deck page, the study session
    "next card" lookup). Subclass of Question so existing handlers
    that just want question fields keep working.
    """

    step: int = 0
    next_due: str
    last_review: str | None = None
    rights: int = 0
    attempts: int = 0


class NewQuestion(BaseModel):
    """Request shape for adding a question — what the route handler
    receives from a form / JSON body. Excludes server-set fields
    (id, user_id, deck_id, created_at, suspended, etc.)."""

    type: QuestionType
    prompt: str = Field(min_length=1)
    answer: str
    topic: str | None = None
    choices: list[str] | None = None
    rubric: str | None = None
    skeleton: str | None = None
    language: str | None = None
