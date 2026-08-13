"""Row ceilings for anonymous accounts.

An anonymous account costs one generation to obtain, so its content
is bounded by policy rather than by cost. Every write path that can
grow an anonymous account reads these two numbers, and the storage
model is derived from them.

The guard takes the caller's connection so the counts and the insert
they gate sit in one transaction.
"""

from __future__ import annotations

ANON_MAX_DECKS = 5
ANON_MAX_QUESTIONS = 200


class RowCapReached(RuntimeError):
    """The account is at its anonymous row cap. Raised before any
    write, so nothing was inserted."""


def refuse_over_row_cap(c, user_id: str, *, new_decks: int = 0, new_questions: int = 0) -> None:
    """Refuse a write that would take an anonymous account past its
    ceiling. A missing users row is left to the write's own foreign
    key; a non-anonymous account has no ceiling."""
    row = c.execute(
        "SELECT is_anonymous FROM users WHERE tailscale_login = ?", (user_id,)
    ).fetchone()
    if row is None or not row["is_anonymous"]:
        return
    decks = c.execute("SELECT COUNT(*) AS n FROM decks WHERE user_id = ?", (user_id,)).fetchone()[
        "n"
    ]
    questions = c.execute(
        "SELECT COUNT(*) AS n FROM questions WHERE user_id = ?", (user_id,)
    ).fetchone()["n"]
    if decks + new_decks > ANON_MAX_DECKS or questions + new_questions > ANON_MAX_QUESTIONS:
        raise RowCapReached(
            f"guest account limit reached: {ANON_MAX_DECKS} decks, "
            f"{ANON_MAX_QUESTIONS} cards. Create an account to add more."
        )
