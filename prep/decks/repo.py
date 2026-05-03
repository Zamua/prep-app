"""Repositories for the decks bounded context.

`DeckRepo` and `QuestionRepo` are the entity-typed interface to the
deck + question tables. Routes / services hold a repo, call methods
on it, and get back `Deck` / `Question` entities (or summaries /
None for the read paths).

In this phase the repos are facades over the existing `prep.db`
accessor functions — same SQL, same row shapes. They exist now so:
1. callers can adopt the entity-based surface incrementally without
   waiting on the full SQL extraction;
2. tests can pin the entity contract against a real sqlite (in-mem
   tmp-path-scoped) before the SQL moves;
3. when phase 5c moves the SQL out of prep/db.py, the repo public
   surface doesn't change — only the internals do.

No service-layer logic here. Repos do reads + writes; orchestration
lives in prep/decks/service.py.
"""

from __future__ import annotations

import json

from prep import db as _legacy_db
from prep.decks.entities import (
    Deck,
    DeckCard,
    DeckSummary,
    DeckType,
    NewQuestion,
    Question,
    QuestionType,
)


class DeckRepo:
    """Read/write access to the `decks` table."""

    def get_or_create(self, user_id: str, name: str) -> int:
        """Return deck id; create with no context_prompt if missing."""
        return _legacy_db.get_or_create_deck(user_id, name)

    def find_id(self, user_id: str, name: str) -> int | None:
        return _legacy_db.find_deck(user_id, name)

    def find_name(self, user_id: str, deck_id: int) -> str | None:
        """Reverse of find_id: deck_id → deck name. Used by routes
        that need to redirect to a deck page after mutating one of
        its questions."""
        from prep.infrastructure.db import cursor

        with cursor() as c:
            row = c.execute(
                "SELECT name FROM decks WHERE id=? AND user_id=?",
                (deck_id, user_id),
            ).fetchone()
        return row["name"] if row else None

    def get_type(self, user_id: str, deck_id: int) -> DeckType | None:
        """Look up a deck's type without paying for the full Deck
        entity. Returns None for unknown / wrong-user deck_ids."""
        from prep.infrastructure.db import cursor

        with cursor() as c:
            row = c.execute(
                "SELECT deck_type FROM decks WHERE id=? AND user_id=?",
                (deck_id, user_id),
            ).fetchone()
        if row is None:
            return None
        return DeckType(row["deck_type"])

    def create(self, user_id: str, name: str, context_prompt: str | None = None) -> int:
        return _legacy_db.create_deck(user_id, name, context_prompt)

    def get_context_prompt(self, user_id: str, name: str) -> str | None:
        return _legacy_db.get_deck_context_prompt(user_id, name)

    def update_context_prompt(self, user_id: str, name: str, context_prompt: str) -> None:
        _legacy_db.update_deck_context_prompt(user_id, name, context_prompt)

    def delete(self, user_id: str, name: str) -> int:
        """Delete deck + all its questions/cards/reviews. Returns the
        deleted deck's id, or 0 if no deck matched."""
        return _legacy_db.delete_deck(user_id, name)

    def list_summaries(self, user_id: str) -> list[DeckSummary]:
        """All decks owned by user, with total + due counts. Used by
        the index page."""
        rows = _legacy_db.list_decks(user_id)
        return [
            DeckSummary(
                id=r["id"],
                name=r["name"],
                total=r["total"] or 0,
                due=r["due"] or 0,
                deck_type=DeckType(r["deck_type"] or "srs"),
            )
            for r in rows
        ]

    def due_breakdown(self, user_id: str) -> list[tuple[str, int]]:
        """[(deck_name, due_count)] across all of user's decks. Used by
        the notifications digest."""
        return _legacy_db.deck_due_breakdown(user_id)

    # ---- trivia-deck-specific helpers ----------------------------------

    def create_trivia(
        self,
        user_id: str,
        name: str,
        *,
        topic: str,
        interval_minutes: int,
    ) -> int:
        """Create a deck with deck_type='trivia'. `topic` is stored in
        the existing context_prompt column (claude reads it during
        generation). `interval_minutes` is per-deck.
        """
        from prep.infrastructure.db import cursor, now

        with cursor() as c:
            cur = c.execute(
                """
                INSERT INTO decks (user_id, name, created_at, context_prompt,
                                   deck_type, notification_interval_minutes)
                VALUES (?, ?, ?, ?, 'trivia', ?)
                """,
                (user_id, name, now(), topic, interval_minutes),
            )
            return cur.lastrowid

    def list_trivia_decks(self) -> list[dict]:
        """All trivia decks across all users. Returned as raw dicts
        because the scheduler needs each row's user_id, interval,
        last_notified_at, and notifications_enabled — fields not all
        on the Deck entity. Includes decks with notifications disabled;
        the scheduler does its own filtering.
        """
        from prep.infrastructure.db import cursor

        with cursor() as c:
            rows = c.execute(
                """
                SELECT id, user_id, name, context_prompt,
                       notification_interval_minutes, last_notified_at,
                       notifications_enabled, notification_ignored_streak
                FROM decks
                WHERE deck_type = 'trivia'
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def record_notification_fire(self, deck_id: int, ts: str, ignored_streak: int) -> None:
        """Stamp last_notified_at + persist the new ignored-streak count
        in a single UPDATE. Called by the scheduler after each fire so
        the next tick reads the updated streak when computing the
        backed-off interval."""
        from prep.infrastructure.db import cursor

        with cursor() as c:
            c.execute(
                """UPDATE decks
                   SET last_notified_at = ?,
                       notification_ignored_streak = ?
                   WHERE id = ?""",
                (ts, ignored_streak, deck_id),
            )

    def reset_ignored_streak_for_deck(self, deck_id: int) -> None:
        """Zero the per-deck backoff counter. Called from the answer
        path the moment a user records a verdict — gives instant
        feedback rather than waiting for the next scheduler tick to
        notice the engagement."""
        from prep.infrastructure.db import cursor

        with cursor() as c:
            c.execute(
                "UPDATE decks SET notification_ignored_streak = 0 WHERE id = ?",
                (deck_id,),
            )

    def set_notification_interval(self, user_id: str, deck_id: int, minutes: int) -> bool:
        """Update a trivia deck's base notification interval. Resets the
        ignored streak so the next push lands at the new base, not at
        whatever backed-off cadence we'd accumulated. Bounds-checked to
        match the create form (1..720). Returns True if a row was
        updated (deck exists, owned by user_id, deck_type='trivia');
        False otherwise — IDOR guard via the user_id + deck_type
        filters."""
        from prep.infrastructure.db import cursor

        if minutes < 1 or minutes > 720:
            raise ValueError(f"interval out of range: {minutes}")
        with cursor() as c:
            cur = c.execute(
                """UPDATE decks
                   SET notification_interval_minutes = ?,
                       notification_ignored_streak = 0
                   WHERE id = ? AND user_id = ? AND deck_type = 'trivia'""",
                (minutes, deck_id, user_id),
            )
            return cur.rowcount > 0

    def set_notifications_enabled(self, user_id: str, deck_id: int, enabled: bool) -> bool:
        """Flip the per-deck notification toggle. Works for both srs
        and trivia decks: srs decks honor it via the digest count
        filter, trivia decks via the per-deck scheduler skip. Returns
        True if a row was updated (deck exists, belongs to `user_id`),
        False otherwise. user_id scoping is the IDOR guard."""
        from prep.infrastructure.db import cursor

        with cursor() as c:
            cur = c.execute(
                "UPDATE decks SET notifications_enabled = ? WHERE id = ? AND user_id = ?",
                (1 if enabled else 0, deck_id, user_id),
            )
            return cur.rowcount > 0


class QuestionRepo:
    """Read/write access to the `questions` table."""

    def add(
        self,
        user_id: str,
        deck_id: int,
        new: NewQuestion,
    ) -> int:
        """Insert a new question + its initial card row, return id."""
        # `choices` on the entity is list[str] | None; the underlying
        # accessor expects the same shape, so we pass through directly.
        return _legacy_db.add_question(
            user_id,
            deck_id,
            new.type.value,
            new.prompt,
            new.answer,
            topic=new.topic,
            choices=new.choices,
            rubric=new.rubric,
            skeleton=new.skeleton,
            language=new.language,
            explanation=new.explanation,
        )

    def update(self, user_id: str, qid: int, new: NewQuestion) -> None:
        """In-place edit. SRS state is preserved across edits."""
        _legacy_db.update_question(
            user_id,
            qid,
            qtype=new.type.value,
            prompt=new.prompt,
            answer=new.answer,
            topic=new.topic,
            choices=new.choices,
            rubric=new.rubric,
            skeleton=new.skeleton,
            language=new.language,
        )

    def get(self, user_id: str, qid: int) -> Question | None:
        row = _legacy_db.get_question(user_id, qid)
        if row is None:
            return None
        return _row_to_question(row)

    def list_in_deck(self, user_id: str, deck_id: int) -> list[DeckCard]:
        """All questions in a deck rendered as deck-page cards (joined
        with SRS state, deck/user context omitted since the caller
        already knows it). Used by the deck view template."""
        rows = _legacy_db.list_questions(user_id, deck_id)
        return [_row_to_deck_card(r) for r in rows]

    def prompts_in_deck(self, user_id: str, deck_id: int) -> list[str]:
        """Just the prompts. Used by the AI deck-transform path to
        give claude the existing-prompts list as context without
        passing full answer keys."""
        return _legacy_db.question_prompts_for_deck(user_id, deck_id)

    def set_suspended(self, user_id: str, qid: int, suspended: bool) -> None:
        _legacy_db.set_suspended(user_id, qid, suspended)


# ---- row-to-entity helpers ----------------------------------------------


def _row_to_question(row: dict) -> Question:
    """Decode a sqlite row dict into a Question entity.

    `choices` arrives as a JSON string (or None); the entity exposes
    list[str] | None, so we decode here.
    """
    choices = row.get("choices")
    if isinstance(choices, str):
        try:
            choices = json.loads(choices)
        except json.JSONDecodeError:
            choices = None
    return Question(
        id=row["id"],
        user_id=row["user_id"],
        deck_id=row["deck_id"],
        type=QuestionType(row["type"]),
        topic=row.get("topic"),
        prompt=row["prompt"],
        choices=choices,
        answer=row["answer"],
        rubric=row.get("rubric"),
        created_at=row["created_at"],
        suspended=bool(row.get("suspended", 0)),
        skeleton=row.get("skeleton"),
        language=row.get("language"),
        explanation=row.get("explanation"),
    )


def _row_to_deck(row: dict) -> Deck:
    """Decode a sqlite row dict into a Deck entity."""
    return Deck(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        created_at=row["created_at"],
        context_prompt=row.get("context_prompt"),
    )


def _row_to_deck_card(row: dict) -> DeckCard:
    """Decode a list_questions row (question fields + SRS state) into
    a DeckCard entity. Tolerates the JSON-encoded `choices` shape."""
    choices = row.get("choices")
    if isinstance(choices, str):
        try:
            choices = json.loads(choices)
        except json.JSONDecodeError:
            choices = None
    return DeckCard(
        id=row["id"],
        type=QuestionType(row["type"]),
        topic=row.get("topic"),
        prompt=row["prompt"],
        choices=choices,
        answer=row["answer"],
        rubric=row.get("rubric"),
        suspended=bool(row.get("suspended", 0)),
        skeleton=row.get("skeleton"),
        language=row.get("language"),
        step=row.get("step") or 0,
        next_due=row["next_due"],
        last_review=row.get("last_review"),
        rights=row.get("rights") or 0,
        attempts=row.get("attempts") or 0,
    )
