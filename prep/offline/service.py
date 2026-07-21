"""Sync orchestration for the offline bounded context.

`sync_batch` implements the POST /api/offline/sync semantics from
docs/OFFLINE.md section 4: cards first (through the same validation
shape the online form gets), then reviews in reviewed_at order across
the whole batch, each replayed through the REAL scheduler
(prep.domain.srs.schedule_review with now=reviewed_at). Per-item
isolation: a bad item lands in status 'rejected' and the rest of the
batch proceeds; only genuine server errors escape as 5xx.

The service adds no scheduling logic of its own -- ordering, clock
clamping, and target resolution live here; every state transition is
the pure domain function, applied by SyncRepo in one transaction per
item alongside its idempotency pin.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from pydantic import ValidationError

from prep.decks.entities import NewQuestion, QuestionType
from prep.decks.repo import DeckRepo
from prep.domain.srs import Verdict
from prep.offline.entities import (
    SyncCardResult,
    SyncNewCard,
    SyncRequest,
    SyncResponse,
    SyncReview,
    SyncReviewResult,
)
from prep.offline.repo import SyncItemRejected, SyncRepo

# Where deck-less offline cards land (get-or-created per user), per
# the protocol: "A deck_id of null files the card into a
# get-or-created deck named inbox."

# graded_by vocabulary -> the grader-notes marker written to the
# append-only reviews log.
_GRADER_NOTES = {
    "auto": "(offline auto)",
    "self": "(offline self-graded)",
}

# Client ids are UUIDs (36 chars); anything wildly longer is a
# protocol violation worth rejecting before it lands in a PK column.
_MAX_CLIENT_ID_CHARS = 64


def sync_batch(
    user_id: str,
    batch: SyncRequest,
    *,
    repo: SyncRepo | None = None,
    deck_repo: DeckRepo | None = None,
) -> SyncResponse:
    """Process one sync batch for the authenticated user. Results come
    back in request order, keyed by client_id."""
    repo = repo or SyncRepo()
    deck_repo = deck_repo or DeckRepo()
    server_now = datetime.now(timezone.utc)

    # Cards first: reviews later in this batch may reference an
    # offline-authored card by card_client_id, and the protocol
    # guarantees cards-before-reviews within a request.
    cards = [_process_card(user_id, item, repo, deck_repo) for item in batch.new_cards]
    reviews = _process_reviews(user_id, batch.reviews, repo, server_now)
    return SyncResponse(cards=cards, reviews=reviews)


# ---- cards --------------------------------------------------------------


def _process_card(
    user_id: str, item: SyncNewCard, repo: SyncRepo, deck_repo: DeckRepo
) -> SyncCardResult:
    try:
        client_id = _require_client_id(item.client_id)
        prior = repo.find_outcome(user_id, client_id)
        if prior is not None:
            if prior["kind"] != "card":
                raise SyncItemRejected("client_id already used by a review item")
            # Retried item: pure lookup, original response.
            return SyncCardResult(
                client_id=client_id, status="created", question_id=prior["question_id"]
            )
        new = _validate_new_card(item)
        deck_id = item.deck_id
        if deck_id is None:
            deck_id = repo.resolve_srs_inbox(user_id, deck_repo)
        elif type(deck_id) is not int or not (1 <= deck_id < 2**63):
            # Deck ids are integers within SQLite's INTEGER range; any
            # other type or magnitude cannot name a deck (and an
            # oversized int would OverflowError at the bind parameter,
            # escaping per-item isolation into a batch 500).
            raise SyncItemRejected("unknown deck_id")
        try:
            qid = repo.create_card(user_id, client_id, deck_id, new.prompt, new.answer)
        except sqlite3.IntegrityError:
            # Concurrent-flush convergence; see the review twin below.
            prior = repo.find_outcome(user_id, client_id)
            if prior is None or prior["kind"] != "card":
                raise
            qid = prior["question_id"]
        return SyncCardResult(client_id=client_id, status="created", question_id=qid)
    except SyncItemRejected as e:
        return SyncCardResult(
            client_id=_echo_client_id(item.client_id), status="rejected", error=str(e)
        )


def _validate_new_card(item: SyncNewCard) -> NewQuestion:
    """The existing validation shape: NewQuestion with type='short',
    prompt and answer required -- the same contract the online manual
    form enforces. A non-string prompt/answer (corrupt outbox row) is
    treated as absent."""
    prompt = item.prompt.strip() if isinstance(item.prompt, str) else ""
    answer = item.answer.strip() if isinstance(item.answer, str) else ""
    if not prompt:
        raise SyncItemRejected("prompt required")
    if not answer:
        raise SyncItemRejected("answer required")
    try:
        return NewQuestion(type=QuestionType.SHORT, prompt=prompt, answer=answer)
    except ValidationError as e:
        raise SyncItemRejected("invalid card") from e


# ---- reviews ------------------------------------------------------------


def _process_reviews(
    user_id: str, items: list[SyncReview], repo: SyncRepo, server_now: datetime
) -> list[SyncReviewResult]:
    """Validate every review, then replay the valid ones in
    (clamped) reviewed_at order across the whole batch, ties broken
    by request position. Results return in request order."""
    results: list[SyncReviewResult | None] = [None] * len(items)
    runnable: list[tuple[datetime, int, dict]] = []

    for i, item in enumerate(items):
        try:
            prepared = _prepare_review(user_id, item, repo, server_now)
        except SyncItemRejected as e:
            results[i] = SyncReviewResult(
                client_id=_echo_client_id(item.client_id), status="rejected", error=str(e)
            )
            continue
        replay = prepared.get("replay")
        if replay is not None:
            results[i] = replay
            continue
        runnable.append((prepared["reviewed_at"], i, prepared))

    runnable.sort(key=lambda entry: (entry[0], entry[1]))
    # client_ids already pinned by an apply THIS batch: a duplicate
    # client_id later in the same request replays the first outcome,
    # exactly as a retried batch would -- prepare-time idempotency
    # lookups cannot see them because nothing has committed yet.
    applied_this_batch: dict[str, str] = {}
    for _reviewed_at, i, p in runnable:
        cid = p["client_id"]
        prior_status = applied_this_batch.get(cid)
        if prior_status is not None:
            results[i] = SyncReviewResult(client_id=cid, status=prior_status)
            continue
        try:
            try:
                status = repo.apply_review(
                    user_id=user_id,
                    client_id=cid,
                    question_id=p["question_id"],
                    verdict=p["verdict"],
                    user_answer=p["user_answer"],
                    reviewed_at=p["reviewed_at"],
                    notes=p["notes"],
                )
            except sqlite3.IntegrityError:
                # Concurrent flush of the same outbox (two restored
                # tabs): both passed the pre-pin lookup, the loser's
                # pin INSERT hit the (user_id, client_id) PK. The
                # winner's committed outcome IS this item's outcome;
                # converge instead of 500ing the batch.
                prior = repo.find_outcome(user_id, cid)
                if prior is None or prior["kind"] != "review":
                    raise
                status = prior["status"]
            applied_this_batch[cid] = status
            results[i] = SyncReviewResult(client_id=cid, status=status)
        except SyncItemRejected as e:
            # Rejects write no pin (the savepoint rolls back), so a
            # later same-id item still gets its own shot -- mirroring
            # what a full-batch retry would do.
            results[i] = SyncReviewResult(client_id=cid, status="rejected", error=str(e))

    # Every slot is filled: each item either rejected at prepare time,
    # replayed from the idempotency pin, or ran above.
    return [r for r in results if r is not None]


def _prepare_review(user_id: str, item: SyncReview, repo: SyncRepo, server_now: datetime) -> dict:
    """Per-item validation + normalization. Returns either
    {'replay': SyncReviewResult} for an idempotent retry, or the
    normalized fields apply_review needs. Raises SyncItemRejected on
    any validation failure."""
    client_id = _require_client_id(item.client_id)
    prior = repo.find_outcome(user_id, client_id)
    if prior is not None:
        if prior["kind"] != "review":
            raise SyncItemRejected("client_id already used by a card item")
        return {"replay": SyncReviewResult(client_id=client_id, status=prior["status"])}

    try:
        verdict = Verdict(item.verdict)
    except (ValueError, TypeError):
        # TypeError covers unhashable garbage in the field.
        raise SyncItemRejected("unknown verdict") from None

    notes = _GRADER_NOTES.get(item.graded_by) if isinstance(item.graded_by, str) else None
    if notes is None:
        raise SyncItemRejected("unknown graded_by")

    reviewed_at = _parse_reviewed_at(item.reviewed_at)
    if reviewed_at > server_now:
        # Clock skew: clamp to server-now before ordering and replay,
        # keeping the original value in the audit trail.
        notes += f" (client reviewed_at {item.reviewed_at} clamped to server now)"
        reviewed_at = server_now

    if item.question_id is not None and item.card_client_id is not None:
        raise SyncItemRejected("give question_id or card_client_id, not both")
    if item.question_id is not None:
        # Question ids are integers; any other type (bool included)
        # cannot name a question and must not reach a bind parameter.
        if type(item.question_id) is not int or not (1 <= item.question_id < 2**63):
            raise SyncItemRejected("unknown question_id")
        question_id = item.question_id
    elif item.card_client_id is not None:
        if not isinstance(item.card_client_id, str):
            raise SyncItemRejected("unknown card_client_id")
        resolved = repo.resolve_card_client_id(user_id, item.card_client_id)
        if resolved is None:
            raise SyncItemRejected("unknown card_client_id")
        question_id = resolved
    else:
        raise SyncItemRejected("question_id or card_client_id required")

    return {
        "client_id": client_id,
        "question_id": question_id,
        "verdict": verdict,
        "user_answer": item.user_answer if isinstance(item.user_answer, str) else "",
        "reviewed_at": reviewed_at,
        "notes": notes,
    }


# ---- shared -------------------------------------------------------------


def _require_client_id(raw: object) -> str:
    """Client ids are strings (UUIDs); anything else is a corrupt
    item that cannot be correlated to an outbox row and rejects the
    same way a missing id does."""
    client_id = raw.strip() if isinstance(raw, str) else ""
    if not client_id:
        raise SyncItemRejected("client_id required")
    if len(client_id) > _MAX_CLIENT_ID_CHARS:
        raise SyncItemRejected("client_id too long")
    return client_id


def _echo_client_id(raw: object) -> str | None:
    """The client_id to echo on a reject: the raw value when it is a
    string (so the client can prune/park the matching outbox row),
    None otherwise -- a non-string id has no outbox row to match and
    must not leak into the typed response."""
    return raw if isinstance(raw, str) else None


def _parse_reviewed_at(raw: object) -> datetime:
    """ISO-8601 with an offset, required. Naive timestamps are
    rejected per item -- a timezone-less instant cannot be ordered
    against server time honestly."""
    if not raw:
        raise SyncItemRejected("reviewed_at required")
    try:
        parsed = datetime.fromisoformat(raw)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        raise SyncItemRejected("reviewed_at is not ISO-8601") from None
    if parsed.tzinfo is None:
        raise SyncItemRejected("reviewed_at must carry a UTC offset")
    return parsed
