"""Application services / use cases for the decks bounded context.

This is the orchestration layer between routes and repos. Routes
should call into here, not repo or temporal_client directly. Routes
get the HTTP-friendly shape they need (no temporal SDK leakage in
the transport layer); tests drive the same code without needing a
TestClient.

Pattern: plain functions, take dependencies as parameters
(repos, optional async client). No classes-as-namespaces — those
add ceremony for a single-method-per-use-case codebase.

Two flavors of use case live here:

1. **Synchronous CRUD with side effects**: deck/question creation,
   delete cascade, suspend toggle. Each calls its repo and may
   touch a second one (e.g., delete_deck cascades to questions
   via FK, but the *invocation* is one call).

2. **Async orchestration (workflows)**: plan-first generation +
   deck-wide transform. Each kicks off a Temporal workflow via the
   passed-in temporal client, returns a workflow id, and provides
   thin wrappers over the signals/queries the route layer needs.
"""

from __future__ import annotations

from typing import Any

from prep.decks.entities import (
    DeckCard,
    DeckSummary,
    NewQuestion,
    Question,
)
from prep.decks.repo import DeckRepo, QuestionRepo

# ============================================================================
# Synchronous CRUD use cases
# ============================================================================


def create_deck(
    repo: DeckRepo,
    user_id: str,
    name: str,
    context_prompt: str | None = None,
) -> int:
    """Create a new deck. Raises sqlite IntegrityError on duplicate name
    for the same user (UNIQUE constraint at the DB level)."""
    return repo.create(user_id, name, context_prompt)


def delete_deck(repo: DeckRepo, user_id: str, name: str) -> int:
    """Delete a deck and (via FK CASCADE) all its questions/cards/reviews.

    Returns the deleted deck's id, or 0 if no match. Caller decides
    whether to surface "not found" as a 404 or a redirect.
    """
    return repo.delete(user_id, name)


def list_user_decks(repo: DeckRepo, user_id: str) -> list[DeckSummary]:
    """All decks the user owns, with total + due counts. Used by
    the index / home page."""
    return repo.list_summaries(user_id)


def add_question(
    repo: QuestionRepo,
    user_id: str,
    deck_id: int,
    new: NewQuestion,
) -> int:
    """Insert a new question + its initial card row."""
    return repo.add(user_id, deck_id, new)


def update_question(
    repo: QuestionRepo,
    user_id: str,
    qid: int,
    new: NewQuestion,
) -> None:
    """Edit an existing question. SRS state is preserved across edits."""
    repo.update(user_id, qid, new)


def get_question(repo: QuestionRepo, user_id: str, qid: int) -> Question | None:
    return repo.get(user_id, qid)


def list_questions_in_deck(repo: QuestionRepo, user_id: str, deck_id: int) -> list[DeckCard]:
    return repo.list_in_deck(user_id, deck_id)


def suspend_question(repo: QuestionRepo, user_id: str, qid: int) -> None:
    repo.set_suspended(user_id, qid, True)


def unsuspend_question(repo: QuestionRepo, user_id: str, qid: int) -> None:
    repo.set_suspended(user_id, qid, False)


# ============================================================================
# Async orchestration — plan-first generation
# ============================================================================
#
# The "client" parameter is a duck-typed shim over the temporal client
# module (prep.temporal_client). Tests pass a fake; routes pass the
# real module. We don't import temporal_client at this layer to keep
# the dependency direction clean (service depends on a Protocol-ish
# interface, not on a concrete adapter).


async def start_plan_generation(
    client: Any,
    *,
    user_id: str,
    deck_id: int,
    deck_name: str,
    prompt: str,
) -> Any:
    """Kick off a PlanGenerate Temporal workflow. Returns the workflow
    handle / metadata object the temporal client gives back — caller
    extracts `.workflow_id` to redirect the user to the plan page."""
    return await client.start_plan_generate(
        user_id=user_id,
        deck_id=deck_id,
        deck_name=deck_name,
        prompt=prompt,
    )


async def submit_plan_feedback(client: Any, wid: str, feedback: str) -> None:
    await client.signal_plan_feedback(wid, feedback)


async def accept_plan(client: Any, wid: str) -> None:
    await client.signal_plan_accept(wid)


async def reject_plan(client: Any, wid: str) -> None:
    await client.signal_plan_reject(wid)


async def get_plan_progress(client: Any, wid: str) -> dict:
    """Returns the workflow's query result (status + plan items so
    far)."""
    return await client.get_plan_progress(wid)


# ============================================================================
# Async orchestration — deck-wide transform
# ============================================================================


async def start_deck_transform(
    client: Any,
    *,
    user_id: str,
    deck_id: int,
    prompt: str,
) -> Any:
    """Kick off a deck-scope Transform Temporal workflow. Waits for an
    apply/reject signal before writing — gives the user a chance to
    review the proposed changes."""
    return await client.start_transform(
        user_id=user_id,
        scope="deck",
        target_id=deck_id,
        prompt=prompt,
    )


async def start_card_transform(
    client: Any,
    *,
    user_id: str,
    qid: int,
    prompt: str,
) -> Any:
    """Kick off a card-scope Transform — auto-applies on completion
    (no apply/reject loop, since per-card improvements are usually
    just the user nudging one prompt at a time)."""
    return await client.start_transform(
        user_id=user_id,
        scope="card",
        target_id=qid,
        prompt=prompt,
    )


async def apply_transform(client: Any, wid: str) -> None:
    await client.signal_apply_transform(wid)


async def reject_transform(client: Any, wid: str) -> None:
    await client.signal_reject_transform(wid)


async def get_transform_progress(client: Any, wid: str) -> dict:
    return await client.get_transform_progress(wid)


async def get_transform_result(client: Any, wid: str) -> dict:
    return await client.get_transform_result(wid)
