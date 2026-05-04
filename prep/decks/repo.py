"""Repositories for the decks bounded context.

`DeckRepo` and `QuestionRepo` are the entity-typed interface to the
deck + question tables. Routes / services hold a repo, call methods
on it, and get back `Deck` / `Question` entities (or summaries /
None for the read paths).

SQL lives here directly — no wrapping over prep.db. Repos do reads
+ writes; orchestration lives in prep/decks/service.py.
"""

from __future__ import annotations

import json

from prep.decks.entities import (
    Deck,
    DeckCard,
    DeckSummary,
    DeckType,
    NewQuestion,
    Question,
    QuestionType,
)
from prep.infrastructure.db import cursor, now


class DeckRepo:
    """Read/write access to the `decks` table."""

    def get_or_create(self, user_id: str, name: str) -> int:
        """Return deck id; create with no context_prompt if missing."""
        with cursor() as c:
            row = c.execute(
                "SELECT id FROM decks WHERE user_id = ? AND name = ?",
                (user_id, name),
            ).fetchone()
            if row:
                return row["id"]
            cur = c.execute(
                "INSERT INTO decks (user_id, name, created_at) VALUES (?, ?, ?)",
                (user_id, name, now()),
            )
            return cur.lastrowid

    def find_id(self, user_id: str, name: str) -> int | None:
        """Read-only deck lookup — does not auto-create. Used for
        ownership checks on workflow status routes where we must NOT
        side-effect on a misrouted poll."""
        with cursor() as c:
            row = c.execute(
                "SELECT id FROM decks WHERE user_id = ? AND name = ?",
                (user_id, name),
            ).fetchone()
        return row["id"] if row else None

    def find_name(self, user_id: str, deck_id: int) -> str | None:
        """Reverse of find_id: deck_id → deck name. Used by routes
        that need to redirect to a deck page after mutating one of
        its questions."""
        with cursor() as c:
            row = c.execute(
                "SELECT name FROM decks WHERE id=? AND user_id=?",
                (deck_id, user_id),
            ).fetchone()
        return row["name"] if row else None

    def get_type(self, user_id: str, deck_id: int) -> DeckType | None:
        """Look up a deck's type without paying for the full Deck
        entity. Returns None for unknown / wrong-user deck_ids."""
        with cursor() as c:
            row = c.execute(
                "SELECT deck_type FROM decks WHERE id=? AND user_id=?",
                (deck_id, user_id),
            ).fetchone()
        if row is None:
            return None
        return DeckType(row["deck_type"])

    def create(self, user_id: str, name: str, context_prompt: str | None = None) -> int:
        """Insert a new deck row. Caller is responsible for validating
        the name (alphanumeric + hyphens, length cap, etc.). Raises
        sqlite3.IntegrityError if the (user_id, name) pair already
        exists."""
        with cursor() as c:
            cur = c.execute(
                "INSERT INTO decks (user_id, name, created_at, context_prompt) VALUES (?, ?, ?, ?)",
                (user_id, name, now(), context_prompt),
            )
            return cur.lastrowid

    def get_context_prompt(self, user_id: str, name: str) -> str | None:
        """Returns the user-supplied context prompt for a deck, or
        None if the deck doesn't exist or has no prompt set yet
        (legacy decks, or a row pre-dating UI creation)."""
        with cursor() as c:
            row = c.execute(
                "SELECT context_prompt FROM decks WHERE user_id = ? AND name = ?",
                (user_id, name),
            ).fetchone()
        if not row:
            return None
        return row["context_prompt"]

    def update_context_prompt(self, user_id: str, name: str, context_prompt: str) -> None:
        with cursor() as c:
            c.execute(
                "UPDATE decks SET context_prompt = ? WHERE user_id = ? AND name = ?",
                (context_prompt, user_id, name),
            )

    def delete(self, user_id: str, name: str) -> int:
        """Delete a deck by name and return the count of rows removed
        (0 or 1). FK CASCADE removes the deck's questions; question
        CASCADEs remove cards / reviews / study_session_answers.
        study_sessions on the deck also cascade. So a single DELETE
        wipes the entire subtree."""
        with cursor() as c:
            cur = c.execute(
                "DELETE FROM decks WHERE user_id = ? AND name = ?",
                (user_id, name),
            )
            return cur.rowcount

    def list_summaries(self, user_id: str) -> list[DeckSummary]:
        """All decks owned by user, with total + due counts. Used by
        the index page."""
        with cursor() as c:
            rows = c.execute(
                """
                SELECT d.id, d.name, d.deck_type,
                       COUNT(q.id) AS total,
                       SUM(CASE WHEN cards.next_due <= ? AND COALESCE(q.suspended,0)=0
                                THEN 1 ELSE 0 END) AS due
                  FROM decks d
                  LEFT JOIN questions q ON q.deck_id = d.id AND q.user_id = d.user_id
                  LEFT JOIN cards ON cards.question_id = q.id
                 WHERE d.user_id = ?
                 GROUP BY d.id
                 ORDER BY d.name
                """,
                (now(), user_id),
            ).fetchall()
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
        """[(deck_name, due_count), ...] for digest body composition.
        Paused decks are excluded — they shouldn't contribute to the
        digest body any more than they contribute to the trigger count.
        Used by the notify scheduler."""
        with cursor() as c:
            rows = c.execute(
                """SELECT d.name, COUNT(c.question_id) AS n
                     FROM decks d
                     LEFT JOIN questions q ON q.deck_id = d.id AND q.user_id = d.user_id
                     LEFT JOIN cards c ON c.question_id = q.id
                                      AND c.next_due <= ?
                                      AND COALESCE(q.suspended, 0) = 0
                    WHERE d.user_id = ?
                      AND COALESCE(d.notifications_enabled, 1) = 1
                    GROUP BY d.id
                   HAVING n > 0
                    ORDER BY n DESC""",
                (now(), user_id),
            ).fetchall()
        return [(r["name"], int(r["n"])) for r in rows]

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

    def list_trivia_decks(self) -> list[Deck]:
        """All trivia decks across all users, as Deck entities. Used by
        the scheduler tick (which needs user_id, interval, last_notified_at,
        notifications_enabled, ignored_streak — all fields on the
        entity now). Includes decks with notifications disabled; the
        scheduler does its own filtering.
        """
        with cursor() as c:
            rows = c.execute(
                """
                SELECT id, user_id, name, created_at, context_prompt,
                       deck_type, notification_interval_minutes, last_notified_at,
                       notifications_enabled, notification_ignored_streak,
                       trivia_session_size
                FROM decks
                WHERE deck_type = 'trivia'
                """
            ).fetchall()
        return [self._row_to_deck(r) for r in rows]

    @staticmethod
    def _row_to_deck(r) -> Deck:
        return Deck(
            id=r["id"],
            user_id=r["user_id"],
            name=r["name"],
            created_at=r["created_at"],
            context_prompt=r["context_prompt"],
            deck_type=DeckType(r["deck_type"] or "srs"),
            notification_interval_minutes=r["notification_interval_minutes"],
            last_notified_at=r["last_notified_at"],
            notifications_enabled=bool(r["notifications_enabled"]),
            notification_ignored_streak=int(r["notification_ignored_streak"] or 0),
            trivia_session_size=int(r["trivia_session_size"] or 3),
        )

    def record_notification_fire(self, deck_id: int, ts: str, ignored_streak: int) -> None:
        """Stamp last_notified_at + persist the new ignored-streak count
        in a single UPDATE. Called by the scheduler after each fire so
        the next tick reads the updated streak when computing the
        backed-off interval."""
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

    def get_trivia_session_size(self, user_id: str, deck_id: int) -> int:
        """Read the per-deck mini-session size for trivia. Returns the
        default (3) if the deck doesn't exist or isn't a trivia deck —
        callers don't differentiate, the route hands it straight to
        pick_session_for_deck."""
        with cursor() as c:
            row = c.execute(
                """SELECT trivia_session_size FROM decks
                   WHERE id = ? AND user_id = ? AND deck_type = 'trivia'""",
                (deck_id, user_id),
            ).fetchone()
        return int(row["trivia_session_size"] or 3) if row else 3

    def set_trivia_session_size(self, user_id: str, deck_id: int, size: int) -> bool:
        """Update the per-deck mini-session size. Bounds-checked 1..20.
        Returns True if a row was updated (deck exists, belongs to user,
        is trivia); False otherwise. user_id + deck_type are the IDOR
        guards."""
        if size < 1 or size > 20:
            raise ValueError(f"session size out of range: {size}")
        with cursor() as c:
            cur = c.execute(
                """UPDATE decks SET trivia_session_size = ?
                   WHERE id = ? AND user_id = ? AND deck_type = 'trivia'""",
                (size, deck_id, user_id),
            )
            return cur.rowcount > 0

    def set_notifications_enabled(self, user_id: str, deck_id: int, enabled: bool) -> bool:
        """Flip the per-deck notification toggle. Works for both srs
        and trivia decks: srs decks honor it via the digest count
        filter, trivia decks via the per-deck scheduler skip. Returns
        True if a row was updated (deck exists, belongs to `user_id`),
        False otherwise. user_id scoping is the IDOR guard."""
        with cursor() as c:
            cur = c.execute(
                "UPDATE decks SET notifications_enabled = ? WHERE id = ?  AND user_id = ?",
                (1 if enabled else 0, deck_id, user_id),
            )
            return cur.rowcount > 0


class QuestionRepo:
    """Read/write access to the `questions` table."""

    def add(self, user_id: str, deck_id: int, new: NewQuestion) -> int:
        """Insert a new question + its initial card row, return id."""
        # `answer` for `multi` may come as a list — store canonically as JSON.
        answer = new.answer
        if isinstance(answer, list):
            answer = json.dumps(answer)
        rubric = new.rubric
        if isinstance(rubric, list):
            rubric = "\n".join(f"- {b}" for b in rubric)
        ts = now()
        with cursor() as c:
            cur = c.execute(
                """
                INSERT INTO questions
                    (user_id, deck_id, type, topic, prompt, choices, answer, rubric, created_at, skeleton, language, explanation)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    deck_id,
                    new.type.value,
                    new.topic,
                    new.prompt,
                    json.dumps(new.choices) if new.choices else None,
                    answer,
                    rubric,
                    ts,
                    new.skeleton if (new.skeleton and new.type.value == "code") else None,
                    new.language if new.type.value == "code" else None,
                    new.explanation,
                ),
            )
            qid = cur.lastrowid
            c.execute(
                "INSERT INTO cards (question_id, step, next_due) VALUES (?, 0, ?)",
                (qid, ts),
            )
            return qid

    def update(self, user_id: str, qid: int, new: NewQuestion) -> None:
        """In-place edit. SRS state is preserved across edits.
        Raises ValueError if no row matches (user_id, qid)."""
        answer = new.answer
        if isinstance(answer, list):
            answer = json.dumps(answer)
        rubric = new.rubric
        if isinstance(rubric, list):
            rubric = "\n".join(f"- {b}" for b in rubric)
        with cursor() as c:
            cur = c.execute(
                """UPDATE questions
                      SET type = ?,
                          topic = ?,
                          prompt = ?,
                          choices = ?,
                          answer = ?,
                          rubric = ?,
                          skeleton = ?,
                          language = ?
                    WHERE id = ? AND user_id = ?""",
                (
                    new.type.value,
                    new.topic,
                    new.prompt,
                    json.dumps(new.choices) if new.choices else None,
                    answer,
                    rubric,
                    new.skeleton if (new.skeleton and new.type.value == "code") else None,
                    new.language if new.type.value == "code" else None,
                    qid,
                    user_id,
                ),
            )
            if cur.rowcount == 0:
                raise ValueError(f"question {qid} not found for user")

    def get(self, user_id: str, qid: int) -> Question | None:
        """Fetch a question, scoped to the user's own questions only.
        Returns None if qid doesn't exist OR belongs to another user
        — same response so we don't leak existence across users."""
        with cursor() as c:
            row = c.execute(
                """
                SELECT q.*, cards.step, cards.next_due
                  FROM questions q LEFT JOIN cards ON cards.question_id = q.id
                 WHERE q.id = ? AND q.user_id = ?
                """,
                (qid, user_id),
            ).fetchone()
        if not row:
            return None
        return _row_to_question(dict(row))

    def list_in_deck(self, user_id: str, deck_id: int) -> list[DeckCard]:
        """All questions in a deck rendered as deck-page cards (joined
        with SRS state, deck/user context omitted since the caller
        already knows it). Used by the deck view template."""
        with cursor() as c:
            rows = c.execute(
                """
                SELECT q.id, q.type, q.topic, q.prompt, q.suspended,
                       q.answer, q.choices, q.rubric, q.skeleton, q.language,
                       cards.step, cards.next_due, cards.last_review,
                       (SELECT COUNT(*) FROM reviews r WHERE r.question_id=q.id) AS attempts,
                       (SELECT COUNT(*) FROM reviews r
                         WHERE r.question_id=q.id AND r.result='right') AS rights
                  FROM questions q
                  LEFT JOIN cards ON cards.question_id = q.id
                 WHERE q.deck_id = ? AND q.user_id = ?
                 ORDER BY cards.next_due ASC, q.id ASC
                """,
                (deck_id, user_id),
            ).fetchall()
        return [_row_to_deck_card(dict(r)) for r in rows]

    def prompts_in_deck(self, user_id: str, deck_id: int) -> list[str]:
        """Just the prompts. Used by the AI deck-transform path to
        give claude the existing-prompts list as context without
        passing full answer keys."""
        with cursor() as c:
            rows = c.execute(
                "SELECT prompt FROM questions WHERE deck_id = ? AND user_id = ?",
                (deck_id, user_id),
            ).fetchall()
        return [r["prompt"] for r in rows]

    def set_suspended(self, user_id: str, qid: int, suspended: bool) -> None:
        with cursor() as c:
            c.execute(
                "UPDATE questions SET suspended = ? WHERE id = ? AND user_id = ?",
                (1 if suspended else 0, qid, user_id),
            )


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
