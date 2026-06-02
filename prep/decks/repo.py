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
    DeckMeta,
    DeckSummary,
    DeckType,
    NewQuestion,
    Question,
    QuestionType,
    TriviaSourceMeta,
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

    def get_meta(self, user_id: str, deck_id: int) -> DeckMeta:
        """Lightweight projection of the deck row used by the deck page
        + the trivia notif-edit popover: notifications toggle, interval,
        session size, context prompt, pinned-state. Returns a `DeckMeta`
        with sensible defaults when the row is missing (so the deck
        page can still render for a freshly-created deck that has no
        row populated yet)."""
        with cursor() as c:
            row = c.execute(
                "SELECT notifications_enabled, notification_interval_minutes, "
                "trivia_session_size, context_prompt, pinned_at "
                "FROM decks WHERE id=? AND user_id=?",
                (deck_id, user_id),
            ).fetchone()
        if not row:
            return DeckMeta(deck_id=deck_id)
        return DeckMeta(
            deck_id=deck_id,
            notifications_enabled=bool(row["notifications_enabled"]),
            interval_minutes=row["notification_interval_minutes"],
            session_size=int(row["trivia_session_size"] or 3),
            context_prompt=row["context_prompt"] or "",
            pinned=row["pinned_at"] is not None,
        )

    def get_trivia_source_meta(self, user_id: str, deck_id: int) -> TriviaSourceMeta | None:
        """Read the trivia split-source projection: inherited interval +
        source topic prompt. Returns None if the deck doesn't exist or
        belongs to another user (IDOR guard via user_id)."""
        with cursor() as c:
            row = c.execute(
                "SELECT notification_interval_minutes, context_prompt"
                " FROM decks WHERE id = ? AND user_id = ?",
                (deck_id, user_id),
            ).fetchone()
        if not row:
            return None
        return TriviaSourceMeta(
            notification_interval_minutes=row["notification_interval_minutes"],
            context_prompt=row["context_prompt"],
        )

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

    def rename(self, user_id: str, old_name: str, new_name: str) -> bool:
        """Rename a deck. Returns True on success, False if the source
        deck doesn't exist or the new name is already taken (the
        UNIQUE(user_id, name) constraint catches collisions)."""
        import sqlite3

        with cursor() as c:
            try:
                cur = c.execute(
                    "UPDATE decks SET name = ? WHERE user_id = ? AND name = ?",
                    (new_name, user_id, old_name),
                )
            except sqlite3.IntegrityError:
                return False
            return cur.rowcount > 0

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
        the index page. Pinned decks float to the top, ordered by
        most-recently-pinned first; the rest fall through to alpha.

        `due` is SRS-only — trivia decks have their own per-deck
        notification cadence, not a per-card due date, so the LEFT
        JOIN on `cards` is gated to srs decks. Without this gate,
        orphaned `cards` rows that pre-fix-#335 trivia decks accumulated
        would inflate the trivia "due" badge on the index page."""
        with cursor() as c:
            rows = c.execute(
                """
                SELECT d.id, d.name, d.deck_type, d.pinned_at,
                       COUNT(q.id) AS total,
                       SUM(CASE WHEN cards.next_due <= ? AND COALESCE(q.suspended,0)=0
                                  AND COALESCE(d.deck_type,'srs') = 'srs'
                                THEN 1 ELSE 0 END) AS due
                  FROM decks d
                  LEFT JOIN questions q ON q.deck_id = d.id AND q.user_id = d.user_id
                  LEFT JOIN cards ON cards.question_id = q.id
                 WHERE d.user_id = ?
                 GROUP BY d.id
                 ORDER BY (d.pinned_at IS NULL), d.pinned_at DESC, d.name
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
                pinned=r["pinned_at"] is not None,
            )
            for r in rows
        ]

    def due_breakdown(self, user_id: str) -> list[tuple[str, int]]:
        """[(deck_name, due_count), ...] for digest body composition.
        Paused decks excluded; trivia decks excluded (they're not
        studied via the SRS flow + have their own per-deck
        notifications). Used by the SRS notify scheduler digest body."""
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
                      AND COALESCE(d.deck_type, 'srs') = 'srs'
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
                       trivia_session_size, notifications_muted_until
                FROM decks
                WHERE deck_type = 'trivia'
                """
            ).fetchall()
        return [self._row_to_deck(r) for r in rows]

    @staticmethod
    def _row_to_deck(r) -> Deck:
        # PRAGMA-tolerant access: columns added by later migrations may
        # be missing on freshly-imported test fixtures. Use dict.get to
        # default cleanly without crashing.
        rd = dict(r) if not isinstance(r, dict) else r
        return Deck(
            id=rd["id"],
            user_id=rd["user_id"],
            name=rd["name"],
            created_at=rd["created_at"],
            context_prompt=rd.get("context_prompt"),
            deck_type=DeckType(rd.get("deck_type") or "srs"),
            notification_interval_minutes=rd.get("notification_interval_minutes"),
            last_notified_at=rd.get("last_notified_at"),
            notifications_enabled=bool(rd.get("notifications_enabled", 1)),
            notification_ignored_streak=int(rd.get("notification_ignored_streak") or 0),
            trivia_session_size=int(rd.get("trivia_session_size") or 3),
            notifications_muted_until=rd.get("notifications_muted_until"),
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

    def mute_notifications_until(self, user_id: str, deck_id: int, until_iso: str | None) -> bool:
        """Silence push notifications for this deck until `until_iso`.
        Pass None to clear an active mute (un-mute). Distinct from the
        permanent on/off `notifications_enabled` toggle — mute is
        time-bounded and auto-expires; enabled is the durable user
        choice. The scheduler checks both: a deck must be enabled AND
        not currently muted to fire a push. Returns True if a row was
        updated."""
        with cursor() as c:
            cur = c.execute(
                "UPDATE decks SET notifications_muted_until = ? WHERE id = ? AND user_id = ?",
                (until_iso, deck_id, user_id),
            )
            return cur.rowcount > 0

    def set_pinned(self, user_id: str, deck_id: int, pinned: bool) -> bool:
        """Pin or unpin the deck. Pinning stamps `pinned_at = now()`;
        unpinning sets it to NULL. The index sorts pinned-first by
        pinned_at DESC, so re-pinning floats a deck back to the top
        of the pinned group. Returns True if a row was updated."""
        with cursor() as c:
            cur = c.execute(
                "UPDATE decks SET pinned_at = ? WHERE id = ? AND user_id = ?",
                (now() if pinned else None, deck_id, user_id),
            )
            return cur.rowcount > 0

    # ---- per-deck FSRS retention override -----------------------------

    def get_desired_retention(self, user_id: str, deck_id: int) -> float | None:
        """Read the deck's retention override, if any. NULL means
        "use the user-level default" — callers should not substitute
        a fallback at the read layer; resolution happens in
        ReviewRepo.record so the deck/user/algorithm order stays in
        one place.
        """
        with cursor() as c:
            row = c.execute(
                "SELECT desired_retention FROM decks WHERE id = ? AND user_id = ?",
                (deck_id, user_id),
            ).fetchone()
        if not row:
            return None
        val = row["desired_retention"]
        return float(val) if val is not None else None

    def set_desired_retention(self, user_id: str, deck_id: int, retention: float | None) -> bool:
        """Set (or clear with None) the deck's retention override.
        Caller clamps to [MIN, MAX] per prep.domain.srs constants.
        Returns True if a row was updated (i.e. the deck exists and
        belongs to the user)."""
        with cursor() as c:
            cur = c.execute(
                "UPDATE decks SET desired_retention = ? WHERE id = ? AND user_id = ?",
                (retention, deck_id, user_id),
            )
            return cur.rowcount > 0


class QuestionRepo:
    """Read/write access to the `questions` table."""

    def add(self, user_id: str, deck_id: int, new: NewQuestion) -> int:
        """Insert a new question. For SRS decks, also seed the FSRS
        cards row so the next study session can pick it up. Trivia
        decks track their queue in `trivia_queue` instead, so no
        cards row is created (an orphan there would inflate the
        index page's due-count via list_summaries).
        """
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
                    (user_id, deck_id, type, topic, prompt, choices, answer, rubric, created_at, skeleton, language, explanation, answer_regex)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    new.answer_regex,
                ),
            )
            qid = cur.lastrowid
            deck_type_row = c.execute(
                "SELECT COALESCE(deck_type, 'srs') AS deck_type FROM decks WHERE id = ?",
                (deck_id,),
            ).fetchone()
            if deck_type_row and deck_type_row["deck_type"] == "srs":
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
                          language = ?,
                          answer_regex = ?
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
                    new.answer_regex,
                    qid,
                    user_id,
                ),
            )
            if cur.rowcount == 0:
                raise ValueError(f"question {qid} not found for user")

    def set_answer_regex(self, user_id: str, qid: int, regex: str | None) -> bool:
        """Update only the answer_regex column. Used by the re-grade
        flow when claude proposes an evolved regex. user_id is the
        IDOR guard. Returns True if a row was updated."""
        with cursor() as c:
            cur = c.execute(
                "UPDATE questions SET answer_regex = ? WHERE id = ? AND user_id = ?",
                (regex, qid, user_id),
            )
            return cur.rowcount > 0

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

    def move_to_deck(self, user_id: str, question_ids: list[int], dest_deck_id: int) -> int:
        """Reassign the given questions to `dest_deck_id`. user_id +
        a join against decks.user_id are the IDOR guards — questions
        owned by another user (or pointing at a foreign destination)
        won't move. Returns the count of rows actually updated."""
        if not question_ids:
            return 0
        placeholders = ",".join("?" * len(question_ids))
        with cursor() as c:
            # Confirm dest deck belongs to user before we touch anything.
            dst = c.execute(
                "SELECT id FROM decks WHERE id = ? AND user_id = ?",
                (dest_deck_id, user_id),
            ).fetchone()
            if not dst:
                return 0
            cur = c.execute(
                f"""UPDATE questions
                       SET deck_id = ?
                     WHERE id IN ({placeholders}) AND user_id = ?""",
                (dest_deck_id, *question_ids, user_id),
            )
            return cur.rowcount

    def list_in_deck(self, user_id: str, deck_id: int) -> list[DeckCard]:
        """All questions in a deck rendered as deck-page cards (joined
        with SRS state, deck/user context omitted since the caller
        already knows it). Used by the deck view template."""
        with cursor() as c:
            rows = c.execute(
                """
                SELECT q.id, q.type, q.topic, q.prompt, q.suspended,
                       q.answer, q.choices, q.rubric, q.skeleton, q.language,
                       q.answer_regex,
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

    def delete(self, user_id: str, qid: int) -> bool:
        """Delete a single question. FK CASCADE drops the cards +
        reviews rows. Returns True if a row was deleted (cross-user
        attempts return False; same shape as not-found)."""
        with cursor() as c:
            cur = c.execute(
                "DELETE FROM questions WHERE id = ? AND user_id = ?",
                (qid, user_id),
            )
            return cur.rowcount > 0


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
        answer_regex=row.get("answer_regex"),
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
        answer_regex=row.get("answer_regex"),
        step=row.get("step") or 0,
        next_due=row["next_due"],
        last_review=row.get("last_review"),
        rights=row.get("rights") or 0,
        attempts=row.get("attempts") or 0,
    )
