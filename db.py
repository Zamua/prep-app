"""SQLite layer for the interview-prep app.

Schema:
  decks       : one row per deck name
  questions   : prompt + type + answer key + rubric, one per card
  reviews     : every grading event, used to compute next-due
  cards       : one-to-one with questions, holds current SRS state

SRS = simplified SM-2:
  wrong              -> next interval = 10 minutes,  ease unchanged
  right (first time) -> 1d
  right after 1d     -> 3d
  right after 3d     -> 7d
  right after 7d     -> 14d
  right after 14d    -> 30d
  beyond 30d         -> stays at 30d
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "data.sqlite"

INTERVAL_LADDER_MINUTES = [
    10,           # wrong -> see again very soon
    24 * 60,      # 1d
    3 * 24 * 60,  # 3d
    7 * 24 * 60,
    14 * 24 * 60,
    30 * 24 * 60,
]

QUESTION_TYPES = {"code", "mcq", "multi", "short"}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def cursor():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with cursor() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS decks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT UNIQUE NOT NULL,
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS questions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                deck_id     INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
                type        TEXT NOT NULL,
                topic       TEXT,
                prompt      TEXT NOT NULL,
                choices     TEXT,
                answer      TEXT NOT NULL,
                rubric      TEXT,
                created_at  TEXT NOT NULL,
                suspended   INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS cards (
                question_id INTEGER PRIMARY KEY REFERENCES questions(id) ON DELETE CASCADE,
                step        INTEGER NOT NULL DEFAULT 0,
                next_due    TEXT NOT NULL,
                last_review TEXT
            );

            CREATE TABLE IF NOT EXISTS reviews (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                question_id  INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
                ts           TEXT NOT NULL,
                result       TEXT NOT NULL,        -- 'right' | 'wrong'
                user_answer  TEXT,
                grader_notes TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_questions_deck ON questions(deck_id);
            CREATE INDEX IF NOT EXISTS idx_cards_due ON cards(next_due);
            CREATE INDEX IF NOT EXISTS idx_reviews_q ON reviews(question_id);
            """
        )


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_or_create_deck(name: str) -> int:
    with cursor() as c:
        row = c.execute("SELECT id FROM decks WHERE name = ?", (name,)).fetchone()
        if row:
            return row["id"]
        cur = c.execute(
            "INSERT INTO decks (name, created_at) VALUES (?, ?)", (name, now())
        )
        return cur.lastrowid


def list_decks() -> list[dict]:
    with cursor() as c:
        rows = c.execute(
            """
            SELECT d.id, d.name,
                   COUNT(q.id) AS total,
                   SUM(CASE WHEN cards.next_due <= ? AND COALESCE(q.suspended,0)=0
                            THEN 1 ELSE 0 END) AS due
              FROM decks d
              LEFT JOIN questions q ON q.deck_id = d.id
              LEFT JOIN cards ON cards.question_id = q.id
             GROUP BY d.id
             ORDER BY d.name
            """,
            (now(),),
        ).fetchall()
        return [dict(r) for r in rows]


def add_question(
    deck_id: int,
    qtype: str,
    prompt: str,
    answer,
    *,
    topic: str | None = None,
    choices: list[str] | None = None,
    rubric=None,
) -> int:
    if qtype not in QUESTION_TYPES:
        raise ValueError(f"unknown type: {qtype}")
    # Normalize: rubric can come in as a list of bullets; flatten to text.
    if isinstance(rubric, list):
        rubric = "\n".join(f"- {b}" for b in rubric)
    # answer for `multi` may come as a list — store canonically as JSON.
    if isinstance(answer, list):
        answer = json.dumps(answer)
    ts = now()
    with cursor() as c:
        cur = c.execute(
            """
            INSERT INTO questions
                (deck_id, type, topic, prompt, choices, answer, rubric, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                deck_id,
                qtype,
                topic,
                prompt,
                json.dumps(choices) if choices else None,
                answer,
                rubric,
                ts,
            ),
        )
        qid = cur.lastrowid
        c.execute(
            "INSERT INTO cards (question_id, step, next_due) VALUES (?, 0, ?)",
            (qid, ts),
        )
        return qid


def list_questions(deck_id: int) -> list[dict]:
    with cursor() as c:
        rows = c.execute(
            """
            SELECT q.id, q.type, q.topic, q.prompt, q.suspended,
                   cards.step, cards.next_due, cards.last_review,
                   (SELECT COUNT(*) FROM reviews r WHERE r.question_id=q.id) AS attempts,
                   (SELECT COUNT(*) FROM reviews r
                     WHERE r.question_id=q.id AND r.result='right') AS rights
              FROM questions q
              LEFT JOIN cards ON cards.question_id = q.id
             WHERE q.deck_id = ?
             ORDER BY cards.next_due ASC, q.id ASC
            """,
            (deck_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def question_prompts_for_deck(deck_id: int) -> list[str]:
    """Used by the generator to avoid duplicates."""
    with cursor() as c:
        rows = c.execute(
            "SELECT prompt FROM questions WHERE deck_id = ?", (deck_id,)
        ).fetchall()
        return [r["prompt"] for r in rows]


def get_question(qid: int) -> dict | None:
    with cursor() as c:
        row = c.execute(
            """
            SELECT q.*, cards.step, cards.next_due
              FROM questions q LEFT JOIN cards ON cards.question_id = q.id
             WHERE q.id = ?
            """,
            (qid,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("choices"):
            d["choices_list"] = json.loads(d["choices"])
        else:
            d["choices_list"] = []
        return d


def due_questions(deck_id: int, limit: int = 3) -> list[dict]:
    """Cards due now, oldest-due first. Falls back to never-attempted-yet."""
    ts = now()
    with cursor() as c:
        rows = c.execute(
            """
            SELECT q.id
              FROM questions q
              JOIN cards ON cards.question_id = q.id
             WHERE q.deck_id = ?
               AND COALESCE(q.suspended, 0) = 0
               AND cards.next_due <= ?
             ORDER BY cards.next_due ASC
             LIMIT ?
            """,
            (deck_id, ts, limit),
        ).fetchall()
        return [get_question(r["id"]) for r in rows]


def record_review(qid: int, result: str, user_answer: str, notes: str = "") -> dict:
    """Record a review and advance/reset the SRS step.

    Returns the new card state.
    """
    if result not in {"right", "wrong"}:
        raise ValueError(f"unknown result: {result}")
    ts = datetime.now(timezone.utc)
    with cursor() as c:
        row = c.execute(
            "SELECT step FROM cards WHERE question_id = ?", (qid,)
        ).fetchone()
        if not row:
            raise ValueError(f"no card for question {qid}")
        step = row["step"]
        if result == "wrong":
            new_step = 0
            interval = INTERVAL_LADDER_MINUTES[0]
        else:
            new_step = min(step + 1, len(INTERVAL_LADDER_MINUTES) - 1)
            interval = INTERVAL_LADDER_MINUTES[new_step]
        next_due = (ts + timedelta(minutes=interval)).isoformat()
        c.execute(
            "INSERT INTO reviews (question_id, ts, result, user_answer, grader_notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (qid, ts.isoformat(), result, user_answer, notes),
        )
        c.execute(
            "UPDATE cards SET step = ?, next_due = ?, last_review = ? "
            "WHERE question_id = ?",
            (new_step, next_due, ts.isoformat(), qid),
        )
        return {"step": new_step, "next_due": next_due, "interval_minutes": interval}


def set_suspended(qid: int, suspended: bool) -> None:
    with cursor() as c:
        c.execute(
            "UPDATE questions SET suspended = ? WHERE id = ?",
            (1 if suspended else 0, qid),
        )


if __name__ == "__main__":
    init()
    print(f"Initialized {DB_PATH}")
