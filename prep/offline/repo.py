"""Snapshot repository for the offline bounded context.

Read-only queries powering GET /api/offline/snapshot. User-scoped
like every repo: every SELECT filters on the authenticated user_id,
so cross-user ids are invisible (IDOR guard, same shape as the decks
and study repos).
"""

from __future__ import annotations

import json

from prep.domain.srs import step_for_stability
from prep.infrastructure.db import cursor
from prep.offline.entities import SnapshotCard, SnapshotDeck


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
