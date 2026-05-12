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

from pydantic import BaseModel

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


def set_notifications_enabled(repo: DeckRepo, user_id: str, deck_id: int, enabled: bool) -> bool:
    """Toggle the deck's notifications flag. When toggling OFF (the
    user is "pausing" the deck), also abandon any active study or
    trivia sessions on the deck — leaving an in-progress session
    live would let the user resume a deck they explicitly silenced.

    Returns True if the deck row was updated (deck exists, belongs to
    user_id), False otherwise. Routes can map False → 404."""
    if not repo.set_notifications_enabled(user_id, deck_id, enabled):
        return False
    if enabled:
        return True
    # Lazy imports to keep the bounded contexts decoupled — these
    # repos live in study/ and trivia/, and importing them at
    # module top-level would couple decks/service.py to those
    # contexts' module load order.
    from prep.study.repo import SessionRepo
    from prep.trivia.repo import TriviaSessionsRepo

    SessionRepo().abandon_all_for_deck(user_id, deck_id)
    TriviaSessionsRepo().abandon_all_for_deck(user_id, deck_id)
    return True


def list_user_decks(repo: DeckRepo, user_id: str) -> list[DeckSummary]:
    """All decks the user owns, with total + due counts. Used by
    the index / home page."""
    return repo.list_summaries(user_id)


def add_question(
    repo: QuestionRepo,
    user_id: str,
    deck_id: int,
    new: NewQuestion,
    *,
    deck_repo: DeckRepo | None = None,
) -> int:
    """Insert a new question + its initial card row. For trivia decks
    also append to the trivia_queue so the new question enters the
    rotation — a manual card add on a trivia deck would otherwise
    drop the card on the floor (the SRS `cards` row is created but
    no notification path picks from `cards`). Pass `deck_repo` so
    the type lookup happens here; routes that already know the deck
    is SRS can skip it."""
    qid = repo.add(user_id, deck_id, new)
    if deck_repo is not None:
        deck_type = deck_repo.get_type(user_id, deck_id)
        if deck_type is not None and deck_type.value == "trivia":
            from prep.trivia.repo import TriviaQueueRepo

            TriviaQueueRepo().append_card(qid, deck_id)
    return qid


def split_deck(
    *,
    deck_repo: DeckRepo,
    question_repo: QuestionRepo,
    user_id: str,
    source_deck_id: int,
    new_deck_name: str,
    question_ids: list[int],
    new_topic_prompt: str | None = None,
) -> int:
    """Manual split flow:

    1. Validate inputs (new name non-empty, no collision, ≥1 card
       selected).
    2. Create the new deck — same type as source. For trivia decks,
       inherit `notification_interval_minutes` and use either the
       provided `new_topic_prompt` or the source's `context_prompt`.
       SRS: just create with optional `context_prompt`.
    3. Reassign the selected questions via `move_to_deck`.
    4. Trivia-specific: abandon any active session on the SOURCE
       deck (avoids stale-card-in-queue weirdness — the moved cards
       no longer belong there). The destination is brand-new so it
       has no active session yet.

    Returns the new deck's id. Raises ValueError on validation
    failure with a user-facing message; route turns it into a 400.
    """
    cleaned_name = (new_deck_name or "").strip()
    if not cleaned_name:
        raise ValueError("new deck name is required")
    if not question_ids:
        raise ValueError("select at least one card to move")
    if deck_repo.find_id(user_id, cleaned_name) is not None:
        raise ValueError(f'a deck named "{cleaned_name}" already exists')

    source_type = deck_repo.get_type(user_id, source_deck_id)
    if source_type is None:
        raise ValueError("source deck not found")

    # Create the destination deck. Trivia and SRS take different
    # paths because trivia decks need an interval + topic.
    if source_type.value == "trivia":
        # Pull source's interval to inherit (sensible default; user
        # can adjust on the new deck after).
        src_meta = deck_repo.get_trivia_source_meta(user_id, source_deck_id)
        interval = (src_meta.notification_interval_minutes or 30) if src_meta else 30
        topic = (
            (new_topic_prompt or "").strip()
            or (src_meta.context_prompt if src_meta else None)
            or cleaned_name
        )
        new_id = deck_repo.create_trivia(
            user_id, cleaned_name, topic=topic, interval_minutes=interval
        )
    else:
        new_id = deck_repo.create(
            user_id, cleaned_name, context_prompt=(new_topic_prompt or "").strip() or None
        )

    moved = question_repo.move_to_deck(user_id, question_ids, new_id)
    if moved == 0:
        # All requested ids belonged to another user / wrong deck —
        # roll back the just-created deck so we don't leave a husk.
        deck_repo.delete(user_id, cleaned_name)
        raise ValueError("none of the selected cards could be moved")

    # Trivia: abandon any active session on the source so the user
    # doesn't get a "resume your session" pointing at moved cards.
    if source_type.value == "trivia":
        from prep.trivia.repo import TriviaSessionsRepo

        sessions = TriviaSessionsRepo()
        existing = sessions.get_active_for_deck(user_id, source_deck_id)
        if existing:
            sessions.replace_active(user_id, source_deck_id, queue=[])
            # replace_active above abandons + creates a fresh empty
            # active row. We immediately mark THAT one completed too
            # so it doesn't show up in the index Continue strip.
            sessions.complete(user_id, source_deck_id)

    return new_id


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
    deck_repo: DeckRepo,
    user_id: str,
    deck_id: int,
    prompt: str,
) -> Any:
    """Kick off a deck-scope Transform Temporal workflow. Waits for an
    apply/reject signal before writing — gives the user a chance to
    review the proposed changes.

    The deck's `context_prompt` ("what this deck is about") is looked
    up here and passed through to the workflow so claude sees the
    deck's overall theme alongside the per-card JSON — same role
    context_prompt plays in the plan-first generation flow."""
    deck_context_prompt = _resolve_deck_context_prompt(deck_repo, user_id, deck_id)
    return await client.start_transform(
        user_id=user_id,
        scope="deck",
        target_id=deck_id,
        prompt=prompt,
        deck_context_prompt=deck_context_prompt,
    )


async def start_card_transform(
    client: Any,
    *,
    deck_repo: DeckRepo,
    question_repo: QuestionRepo,
    user_id: str,
    qid: int,
    prompt: str,
) -> Any:
    """Kick off a card-scope Transform — auto-applies on completion
    (no apply/reject loop, since per-card improvements are usually
    just the user nudging one prompt at a time).

    Looks up the question's owning deck so we can thread that deck's
    `context_prompt` through to claude — a single-card edit benefits
    from knowing the deck's overall theme too, not just the card
    JSON in isolation."""
    deck_context_prompt = ""
    q = question_repo.get(user_id, qid)
    if q is not None:
        deck_context_prompt = _resolve_deck_context_prompt(deck_repo, user_id, q.deck_id)
    return await client.start_transform(
        user_id=user_id,
        scope="card",
        target_id=qid,
        prompt=prompt,
        deck_context_prompt=deck_context_prompt,
    )


def _resolve_deck_context_prompt(deck_repo: DeckRepo, user_id: str, deck_id: int) -> str:
    """Look up a deck's context_prompt by id. Returns "" for legacy
    decks without one set, or when the deck doesn't exist / belongs
    to another user (defense-in-depth — the IDOR guard at the route
    layer should have caught that already)."""
    name = deck_repo.find_name(user_id, deck_id)
    if not name:
        return ""
    return deck_repo.get_context_prompt(user_id, name) or ""


async def apply_transform(client: Any, wid: str) -> None:
    await client.signal_apply_transform(wid)


async def reject_transform(client: Any, wid: str) -> None:
    await client.signal_reject_transform(wid)


async def get_transform_progress(client: Any, wid: str) -> dict:
    return await client.get_transform_progress(wid)


async def get_transform_result(client: Any, wid: str) -> dict:
    return await client.get_transform_result(wid)


# ----------------------------------------------------------------------------
# Transform view rendering context
# ----------------------------------------------------------------------------
#
# `build_transform_view_ctx` composes the dict the transform.html
# template expects from the already-fetched workflow progress + a few
# repo lookups. Lifted out of `routes.transform_view` so the diff
# building / deck-name resolution / move grouping has somewhere to be
# tested without firing up Temporal.


class TransformModificationDiff(BaseModel):
    """Per-modification before/after pair, used by the transform preview
    template. Old and new shapes are dict-of-strings so the template
    can iterate fields uniformly."""

    question_id: int
    deck_name: str
    old: dict[str, str]
    new: dict[str, str]


class TransformViewCtx(BaseModel):
    """Full template context for `transform_view`. Values match the
    keys the existing template reads, just typed.

    Keep this aligned with `templates/transform.html` field accesses;
    rename or add fields here when the template changes."""

    deck_name: str
    modification_diffs: list[TransformModificationDiff]
    deletion_decks: dict[int, str]
    move_source_decks: dict[int, str]
    deck_id_to_name: dict[int, str]


def _question_to_diff_dict(q: Question) -> dict[str, str]:
    """Old-side of a modification diff: every editable field as a
    string (or empty-string when null)."""
    return {
        "type": q.type.value,
        "topic": q.topic or "",
        "prompt": q.prompt,
        "answer": q.answer,
        "rubric": q.rubric or "",
        "skeleton": q.skeleton or "",
        "language": q.language or "",
        "explanation": q.explanation or "",
        "answer_regex": q.answer_regex or "",
    }


def _modification_to_new_dict(m: dict, old: Question) -> dict[str, str]:
    """New-side of a modification diff: claude's proposed value when
    present, else fall through to the old value. Same keys as the old
    side so the template can `for k in old` and align."""
    return {
        "type": m.get("type") or old.type.value,
        "topic": (m.get("topic") or old.topic or "") or "",
        "prompt": m.get("prompt") or old.prompt,
        "answer": m.get("answer") or old.answer,
        "rubric": m.get("rubric") if m.get("rubric") is not None else (old.rubric or ""),
        "skeleton": m.get("skeleton") if m.get("skeleton") is not None else (old.skeleton or ""),
        "language": m.get("language") if m.get("language") is not None else (old.language or ""),
        "explanation": m.get("explanation")
        if m.get("explanation") is not None
        else (old.explanation or ""),
        "answer_regex": m.get("answer_regex")
        if m.get("answer_regex") is not None
        else (old.answer_regex or ""),
    }


def build_transform_view_ctx(
    *,
    deck_repo: DeckRepo,
    question_repo: QuestionRepo,
    user_id: str,
    scope: str,
    target_id: int,
    progress: dict | None,
) -> TransformViewCtx:
    """Build the context dict for the transform-preview page.

    - Resolve the deck name for the back link (by deck_id for deck
      scope; via the question's deck for card scope).
    - Build a per-modification diff (OLD live shape from the DB next
      to claude's proposed NEW shape).
    - For reorganize plans, pre-resolve source-deck names for deletions
      + card_moves so the template can group changes by deck without
      doing per-row lookups in jinja.

    Pure-ish: depends only on repos, the user_id scope, and the
    workflow progress dict. The caller (route) handles the temporal
    fetch + terminal-status fallback.
    """
    plan = (progress or {}).get("plan") or {}
    deck_id_to_name: dict[int, str] = {s.id: s.name for s in deck_repo.list_summaries(user_id)}

    deck_name = ""
    if scope == "deck":
        deck_name = deck_repo.find_name(user_id, target_id) or ""
    else:
        q = question_repo.get(user_id, target_id)
        if q is not None:
            deck_name = deck_repo.find_name(user_id, q.deck_id) or ""

    def _deck_for_qid(qid: int) -> str:
        q = question_repo.get(user_id, qid)
        return deck_id_to_name.get(q.deck_id, "") if q is not None else ""

    modification_diffs: list[TransformModificationDiff] = []
    for m in plan.get("modifications") or []:
        qid = m.get("question_id")
        if not qid:
            continue
        old = question_repo.get(user_id, qid)
        if old is None:
            continue
        modification_diffs.append(
            TransformModificationDiff(
                question_id=qid,
                deck_name=deck_id_to_name.get(old.deck_id, ""),
                old=_question_to_diff_dict(old),
                new=_modification_to_new_dict(m, old),
            )
        )

    deletion_decks: dict[int, str] = {
        qid: _deck_for_qid(qid) for qid in (plan.get("deletions") or [])
    }
    move_source_decks: dict[int, str] = {
        mv.get("question_id"): _deck_for_qid(mv.get("question_id"))
        for mv in (plan.get("card_moves") or [])
        if mv.get("question_id")
    }

    return TransformViewCtx(
        deck_name=deck_name,
        modification_diffs=modification_diffs,
        deletion_decks=deletion_decks,
        move_source_decks=move_source_decks,
        deck_id_to_name=deck_id_to_name,
    )
