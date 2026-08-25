"""Rows the DB-backed oracles need beyond the shared profiles in
`prep/dev/parity_seed.py`: the parity user on its own, and an
anonymous account written past the cap guard.
"""

from __future__ import annotations

from tests.parity.oracles import PARITY_NOW, PARITY_USER

ANON_ID = "anon:" + "ab" * 16


def upsert_parity_user(login: str = PARITY_USER) -> dict:
    from prep.auth.repo import UserRepo
    from prep.dev.parity_seed import create_user

    create_user(login)
    return UserRepo().get_by_external_id(login)


def seed_anonymous(external_id: str = ANON_ID, *, questions: int = 0) -> str:
    """An anonymous account row, optionally holding `questions` cards
    in one deck (written directly: the cap guard would refuse the
    repository path past the ceiling)."""
    from prep.infrastructure.db import cursor

    ts = PARITY_NOW.isoformat()
    with cursor() as c:
        c.execute(
            "INSERT INTO users (tailscale_login, display_name, email, created_at, last_seen_at,"
            " is_anonymous) VALUES (?, 'Guest', NULL, ?, ?, 1)",
            (external_id, ts, ts),
        )
        if questions:
            deck = c.execute(
                "INSERT INTO decks (user_id, name, display_name, created_at)"
                " VALUES (?, 'full', 'Full deck', ?)",
                (external_id, ts),
            ).lastrowid
            for i in range(questions):
                qid = c.execute(
                    "INSERT INTO questions (user_id, deck_id, type, prompt, answer, created_at)"
                    " VALUES (?, ?, 'short', ?, ?, ?)",
                    (external_id, deck, f"Question {i}?", f"answer {i}", ts),
                ).lastrowid
                c.execute(
                    "INSERT INTO cards (question_id, step, next_due) VALUES (?, 0, ?)", (qid, ts)
                )
    return external_id
