"""Repositories for the offline bounded context.

`SnapshotRepo`: read-only queries powering GET /api/offline/snapshot.
`SyncRepo`: the per-item writes behind POST /api/offline/sync.

User-scoped like every repo: every statement filters on the
authenticated user_id, so cross-user ids are invisible (IDOR guard,
same shape as the decks and study repos).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from prep.domain.srs import CardSRSState, Verdict, schedule_review, step_for_stability
from prep.infrastructure.db import cursor, now
from prep.offline.entities import INBOX_DECK_NAME, SnapshotCard, SnapshotDeck


class SyncItemRejected(Exception):
    """A sync item failed validation against server state. The service
    catches this per item and reports status 'rejected' -- it never
    fails the batch. Anything else raised out of a SyncRepo method is
    a real server error and propagates to a 5xx, which tells the
    client to keep the item queued and retry."""


class SnapshotRepo:
    """Read access for the offline snapshot: the user's SRS decks and
    their non-suspended questions joined with SRS state."""

    def decks(self, user_id: str) -> list[SnapshotDeck]:
        """The user's SRS decks. Trivia decks are excluded: offline
        covers SRS study only."""
        with cursor() as c:
            rows = c.execute(
                """
                SELECT id, name, display_name
                  FROM decks
                 WHERE user_id = ?
                   AND COALESCE(deck_type, 'srs') = 'srs'
                 ORDER BY COALESCE(display_name, name)
                """,
                (user_id,),
            ).fetchall()
        return [
            SnapshotDeck(id=r["id"], name=r["name"], display_name=r["display_name"]) for r in rows
        ]

    def cards(self, user_id: str) -> list[SnapshotCard]:
        """Every non-suspended question in the user's SRS decks, with
        its SRS view. Every card ships, not just currently-due ones:
        multi-day offline needs the cards that become due later in the
        window, and the whole payload is small text. The join through
        decks enforces both the SRS-only scope and (belt and
        suspenders with the questions.user_id filter) ownership."""
        with cursor() as c:
            rows = c.execute(
                """
                SELECT q.id AS question_id, q.deck_id, q.type, q.prompt,
                       q.choices, q.answer, q.answer_regex, q.rubric,
                       q.skeleton, q.explanation,
                       cards.next_due, cards.stability
                  FROM questions q
                  JOIN decks d ON d.id = q.deck_id AND d.user_id = q.user_id
                  JOIN cards ON cards.question_id = q.id
                 WHERE q.user_id = ?
                   AND COALESCE(q.suspended, 0) = 0
                   AND COALESCE(d.deck_type, 'srs') = 'srs'
                 ORDER BY q.id
                """,
                (user_id,),
            ).fetchall()
        return [_row_to_card(dict(r)) for r in rows]


def _row_to_card(row: dict) -> SnapshotCard:
    """Decode a snapshot row into a SnapshotCard. `choices` arrives as
    a JSON string (or None); the entity exposes list[str] | None, same
    convention as the decks repo."""
    choices = row.get("choices")
    if isinstance(choices, str):
        try:
            choices = json.loads(choices)
        except json.JSONDecodeError:
            choices = None
    return SnapshotCard(
        question_id=row["question_id"],
        deck_id=row["deck_id"],
        type=row["type"],
        prompt=row["prompt"],
        choices=choices,
        answer=row["answer"],
        answer_regex=row.get("answer_regex"),
        rubric=row.get("rubric"),
        skeleton=row.get("skeleton"),
        explanation=row.get("explanation"),
        step=step_for_stability(row.get("stability")),
        next_due=row["next_due"],
    )


class SyncRepo:
    """Write access for POST /api/offline/sync.

    Each mutating method is ONE item's effect in ONE transaction (the
    per-item savepoint the protocol requires): the domain write and
    its offline_sync_idempotency row commit together or not at all,
    so a crash mid-batch can never leave an effect a retried batch
    would duplicate. The cursor() context manager gives exactly that
    scope -- commit on clean exit, rollback on raise."""

    # ---- idempotency reads ----------------------------------------

    def find_outcome(self, user_id: str, client_id: str) -> dict | None:
        """The pinned outcome of a previously-processed item, or None.
        A hit means the retried item replays as this pure lookup."""
        with cursor() as c:
            row = c.execute(
                "SELECT kind, status, question_id FROM offline_sync_idempotency "
                " WHERE user_id = ? AND client_id = ?",
                (user_id, client_id),
            ).fetchone()
        if not row:
            return None
        return {
            "kind": row["kind"],
            "status": row["status"],
            "question_id": row["question_id"],
        }

    def resolve_card_client_id(self, user_id: str, card_client_id: str) -> int | None:
        """Map an offline-authored card's client UUID to the question
        the sync path created for it (this batch or an earlier one)."""
        with cursor() as c:
            row = c.execute(
                "SELECT question_id FROM offline_sync_idempotency "
                " WHERE user_id = ? AND client_id = ? AND kind = 'card' "
                "   AND status = 'created'",
                (user_id, card_client_id),
            ).fetchone()
        return row["question_id"] if row else None

    # ---- item writes ----------------------------------------------

    def resolve_srs_inbox(self, user_id: str, deck_repo) -> int:
        """The inbox deck for deck_id-less offline cards, SRS-scoped.

        DeckRepo.get_or_create matches by name alone; if the user
        already owns a TRIVIA deck named "inbox", handing its id to
        create_card would bounce every inbox card off the SRS-only
        check with a misleading "unknown deck_id". Prefer an existing
        SRS deck named "inbox"; when the name is taken by a non-SRS
        deck, fall back to a distinct suffixed SRS inbox."""
        with cursor() as c:
            row = c.execute(
                "SELECT id FROM decks WHERE user_id = ? AND name = ? AND deck_type = 'srs'",
                (user_id, INBOX_DECK_NAME),
            ).fetchone()
            if row:
                return row["id"]
            taken = c.execute(
                "SELECT 1 FROM decks WHERE user_id = ? AND name = ?",
                (user_id, INBOX_DECK_NAME),
            ).fetchone()
        name = f"{INBOX_DECK_NAME}-offline" if taken else INBOX_DECK_NAME
        return deck_repo.get_or_create(user_id, name)

    def create_card(
        self, user_id: str, client_id: str, deck_id: int, prompt: str, answer: str
    ) -> int:
        """Insert an offline-authored card: a type='short' question
        plus its cards row, due immediately (matching online manual
        authoring), plus the idempotency pin -- one transaction.

        Mirrors QuestionRepo.add for the offline subset (short cards
        have no choices/skeleton/language). The deck ownership + SRS
        check lives inside the same transaction; offline covers SRS
        decks only, and an unknown/foreign/trivia deck_id rejects with
        the same error (no cross-user existence leak)."""
        ts = now()
        with cursor() as c:
            deck = c.execute(
                "SELECT id FROM decks WHERE id = ? AND user_id = ? "
                "  AND COALESCE(deck_type, 'srs') = 'srs'",
                (deck_id, user_id),
            ).fetchone()
            if not deck:
                raise SyncItemRejected("unknown deck_id")
            cur = c.execute(
                "INSERT INTO questions (user_id, deck_id, type, prompt, answer, created_at) "
                "VALUES (?, ?, 'short', ?, ?, ?)",
                (user_id, deck_id, prompt, answer, ts),
            )
            qid = cur.lastrowid
            c.execute(
                "INSERT INTO cards (question_id, step, next_due) VALUES (?, 0, ?)",
                (qid, ts),
            )
            c.execute(
                "INSERT INTO offline_sync_idempotency "
                " (user_id, client_id, kind, status, question_id, created_at) "
                "VALUES (?, ?, 'card', 'created', ?, ?)",
                (user_id, client_id, qid, ts),
            )
            return qid

    def apply_review(
        self,
        user_id: str,
        client_id: str,
        question_id: int,
        verdict: Verdict,
        user_answer: str,
        reviewed_at: datetime,
        notes: str,
    ) -> str:
        """Replay one offline review -- one transaction. Returns the
        status: 'applied' (scheduler ran at now=reviewed_at) or
        'logged_no_reschedule' (audit row only: another review already
        advanced the card at a later-or-equal timestamp, so
        last-writer-wins keeps the card state and FSRS never sees
        negative elapsed time). Either way the review row lands in the
        append-only log with the CLIENT's timestamp -- the study
        effort is real history."""
        with cursor() as c:
            row = c.execute(
                "SELECT cards.stability, cards.difficulty, cards.fsrs_state, cards.last_review "
                "  FROM questions q JOIN cards ON cards.question_id = q.id "
                " WHERE q.id = ? AND q.user_id = ?",
                (question_id, user_id),
            ).fetchone()
            if not row:
                raise SyncItemRejected("unknown question_id")

            last_review = None
            if row["last_review"]:
                last_review = datetime.fromisoformat(row["last_review"])
                if last_review.tzinfo is None:
                    last_review = last_review.replace(tzinfo=timezone.utc)

            reviewed_at_iso = reviewed_at.isoformat()
            if last_review is not None and reviewed_at <= last_review:
                status = "logged_no_reschedule"
                c.execute(
                    "INSERT INTO reviews (question_id, ts, result, user_answer, grader_notes) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (question_id, reviewed_at_iso, verdict.value, user_answer, notes),
                )
            else:
                status = "applied"
                state = CardSRSState(
                    stability=row["stability"],
                    difficulty=row["difficulty"],
                    fsrs_state=row["fsrs_state"] or 1,
                    last_review=last_review,
                )
                # Same retention resolution as the online record()
                # path: deck override -> user default -> algorithm
                # default (inside schedule_review).
                ret_row = c.execute(
                    """SELECT d.desired_retention AS deck_ret,
                              u.desired_retention AS user_ret
                         FROM questions q
                         JOIN decks d ON d.id = q.deck_id
                         JOIN users u ON u.tailscale_login = q.user_id
                        WHERE q.id = ?""",
                    (question_id,),
                ).fetchone()
                effective_retention = None
                if ret_row is not None:
                    effective_retention = ret_row["deck_ret"]
                    if effective_retention is None:
                        effective_retention = ret_row["user_ret"]
                scheduled = schedule_review(
                    state, verdict, now=reviewed_at, desired_retention=effective_retention
                )
                c.execute(
                    "INSERT INTO reviews (question_id, ts, result, user_answer, grader_notes) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (question_id, reviewed_at_iso, verdict.value, user_answer, notes),
                )
                c.execute(
                    """UPDATE cards
                          SET step = ?, next_due = ?, last_review = ?,
                              stability = ?, difficulty = ?, fsrs_state = ?
                        WHERE question_id = ?""",
                    (
                        scheduled.step_bucket,
                        scheduled.next_due.isoformat(),
                        reviewed_at_iso,
                        scheduled.state.stability,
                        scheduled.state.difficulty,
                        scheduled.state.fsrs_state,
                        question_id,
                    ),
                )
            c.execute(
                "INSERT INTO offline_sync_idempotency "
                " (user_id, client_id, kind, status, question_id, created_at) "
                "VALUES (?, ?, 'review', ?, ?, ?)",
                (user_id, client_id, status, question_id, now()),
            )
            return status
