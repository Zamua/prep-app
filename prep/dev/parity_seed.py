"""Parity seed profiles (docs/PARITY-GATE.md C6).

`POST /_parity/seed` wipes one login's rows, recreates the user in the
parity timezone and inserts a named profile through the repositories,
so the same call works on every target and survives migrations.
Every timestamp is relative to the process clock, which a parity
target pins with `PREP_FAKE_NOW`.

`prep.app` mounts it through `register(app)` under `PREP_PARITY_MODE=1`
only.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from prep.agent.routes import _require_internal_token
from prep.auth.merge import discover_user_scoped_tables
from prep.auth.repo import UserRepo
from prep.decks.entities import NewQuestion, QuestionType
from prep.decks.repo import DeckRepo, QuestionRepo
from prep.infrastructure import clock
from prep.infrastructure.db import cursor
from prep.notify.repo import NotificationLogRepo
from prep.study.repo import SessionRepo
from prep.trivia.repo import TriviaQueueRepo
from prep.workflows.entities import WorkflowType
from prep.workflows.repo import ActiveWorkflowsRepo

PARITY_TZ = "America/New_York"
PARITY_DISPLAY_NAME = "Parity"
DEVICE_LABEL = "iPhone"

SEED_PATH = "/_parity/seed"


def at(**delta) -> str:
    """The process clock shifted by `timedelta(**delta)`, in the column
    format `db.now()` writes."""
    return (clock.now() + timedelta(**delta)).isoformat()


class SeedRequest(BaseModel):
    user: str
    profile: str


# ---- wipe + user ----------------------------------------------------------


def wipe_user(user: str) -> None:
    with cursor() as c:
        tables = discover_user_scoped_tables(c)
        for table, columns in tables.items():
            for column in columns:
                c.execute(f'DELETE FROM "{table}" WHERE "{column}" = ?', (user,))
        c.execute("DELETE FROM users WHERE tailscale_login = ?", (user,))
        # Dropping the AUTOINCREMENT counters restarts ids at max(rowid)+1, so
        # a re-seed on the same server hands out the ids the first seed did
        # and a golden shot of a card number holds on any target.
        for table in tables:
            c.execute("DELETE FROM sqlite_sequence WHERE name = ?", (table,))


def create_user(user: str) -> None:
    repo = UserRepo()
    repo.upsert(user, email=user, display_name=PARITY_DISPLAY_NAME)
    prefs = repo.get_notification_prefs(user)
    prefs["tz"] = PARITY_TZ
    repo.set_notification_prefs(user, prefs)


# ---- column pins ----------------------------------------------------------


def _pin_card(qid: int, *, due: str, step: int = 0, last_review: str | None = None) -> None:
    with cursor() as c:
        c.execute(
            "UPDATE cards SET next_due = ?, step = ?, last_review = ? WHERE question_id = ?",
            (due, step, last_review, qid),
        )


def _add_review(qid: int, *, ts: str, result: str, user_answer: str) -> None:
    with cursor() as c:
        c.execute(
            "INSERT INTO reviews (question_id, ts, result, user_answer) VALUES (?, ?, ?, ?)",
            (qid, ts, result, user_answer),
        )


def _pin_session(sid: str, *, last_active: str, created_at: str | None = None) -> None:
    with cursor() as c:
        c.execute(
            "UPDATE study_sessions SET last_active = ?, created_at = ? WHERE id = ?",
            (last_active, created_at or last_active, sid),
        )


def _answer_in_session(sid: str, qid: int, *, answered_at: str, result: str) -> None:
    with cursor() as c:
        c.execute(
            "INSERT INTO study_session_answers (session_id, question_id, answered_at, result)"
            " VALUES (?, ?, ?, ?)",
            (sid, qid, answered_at, result),
        )


def _pin_pinned_at(deck_id: int, pinned_at: str) -> None:
    with cursor() as c:
        c.execute("UPDATE decks SET pinned_at = ? WHERE id = ?", (pinned_at, deck_id))


def _pin_notification(note_id: int, sent_at: str) -> None:
    with cursor() as c:
        c.execute("UPDATE notifications_log SET sent_at = ? WHERE id = ?", (sent_at, note_id))


def _pin_workflow_started(workflow_id: str, started_at: str) -> None:
    with cursor() as c:
        c.execute(
            "UPDATE active_workflows SET started_at = ? WHERE workflow_id = ?",
            (started_at, workflow_id),
        )


def _insert_api_token(user: str, *, plaintext: str, label: str, created_at: str) -> int:
    """A PAT with a known plaintext, so its masked prefix is stable;
    `ApiTokenRepo.issue` draws the secret at random."""
    from prep.api.repo import _hash, _mask

    with cursor() as c:
        return c.execute(
            "INSERT INTO api_tokens (user_id, token_hash, label, key_prefix, created_at,"
            " last_used_at) VALUES (?, ?, ?, ?, ?, NULL)",
            (user, _hash(plaintext), label, _mask(plaintext), created_at),
        ).lastrowid


# ---- card sets --------------------------------------------------------------


def _q(qtype: str, prompt: str, answer, **extra) -> NewQuestion:
    return NewQuestion(type=QuestionType(qtype), prompt=prompt, answer=answer, **extra)


def _capitals_cards() -> list[tuple[str, NewQuestion, dict]]:
    """(key, question, pins) for the World Capitals deck: one of each
    type, a suspended card, distinct due minutes so the due order is
    total."""
    return [
        (
            "mcq",
            _q(
                "mcq",
                "Which city is the capital of Australia?",
                "Canberra",
                choices=["Sydney", "Canberra", "Melbourne", "Perth"],
                topic="oceania",
            ),
            {"due": at(hours=-3), "step": 2, "last_review": at(days=-2)},
        ),
        (
            "short_regex",
            _q(
                "short",
                "Capital of Kenya?",
                "Nairobi",
                answer_regex="(?i)^\\s*nairobi\\s*$",
                topic="africa",
            ),
            {"due": at(hours=-2), "step": 1, "last_review": at(days=-1)},
        ),
        (
            "multi",
            _q(
                "multi",
                "Which of these are national capitals?",
                json.dumps(["Ottawa", "Lima"]),
                choices=["Ottawa", "Toronto", "Lima", "Rio de Janeiro"],
                topic="americas",
            ),
            {"due": at(hours=-1)},
        ),
        (
            "code",
            _q(
                "code",
                "Return the capital for a country code from `table`, or `None` when unknown.",
                "def capital(code, table):\n    return table.get(code)\n",
                language="python",
                skeleton="def capital(code, table):\n    ...\n",
                rubric="- Uses dict.get\n- Returns None on a miss",
                topic="python",
            ),
            {"due": at(days=2), "step": 3, "last_review": at(days=-5)},
        ),
        (
            "short_plain",
            _q("short", "Capital of Peru?", "Lima", topic="americas"),
            {"due": at(days=5), "step": 4, "last_review": at(days=-9)},
        ),
        (
            "suspended",
            _q("short", "Capital of Ghana?", "Accra", topic="africa"),
            {"due": at(hours=-4), "suspended": True},
        ),
    ]


def _insert_cards(user: str, deck_id: int, cards) -> dict[str, int]:
    qrepo = QuestionRepo()
    ids: dict[str, int] = {}
    for key, new, pins in cards:
        qid = qrepo.add(user, deck_id, new)
        ids[key] = qid
        _pin_card(
            qid,
            due=pins["due"],
            step=pins.get("step", 0),
            last_review=pins.get("last_review"),
        )
        if pins.get("suspended"):
            qrepo.set_suspended(user, qid, True)
    return ids


# ---- profiles ---------------------------------------------------------------


def profile_empty(user: str) -> dict:
    return {}


def profile_reader(user: str) -> dict:
    decks = DeckRepo()
    sessions = SessionRepo()

    a = decks.create(
        user,
        "world-capitals",
        context_prompt="Capital cities of the world, one card per country.",
        display_name="World Capitals",
    )
    a_ids = _insert_cards(user, a, _capitals_cards())
    _add_review(a_ids["mcq"], ts=at(days=-2), result="right", user_answer="Canberra")
    _add_review(a_ids["mcq"], ts=at(days=-6), result="wrong", user_answer="Sydney")
    _add_review(a_ids["short_regex"], ts=at(days=-1), result="right", user_answer="Nairobi")
    _add_review(a_ids["code"], ts=at(days=-5), result="right", user_answer="table.get(code)")

    b = decks.create(
        user,
        "distributed-systems",
        context_prompt="Consensus, replication and failure detection.",
        display_name="Distributed Systems",
    )
    b_ids = _insert_cards(
        user,
        b,
        [
            (
                "raft",
                _q(
                    "short",
                    "In Raft, what does a follower do when its election timeout elapses?",
                    "It becomes a candidate, increments its term and requests votes.",
                    topic="consensus",
                ),
                {"due": at(minutes=-30), "step": 1, "last_review": at(days=-1)},
            ),
            (
                "quorum",
                _q(
                    "mcq",
                    "With N=5 replicas, the smallest write quorum that still overlaps every read quorum of 3 is:",
                    "3",
                    choices=["2", "3", "4", "5"],
                    topic="replication",
                ),
                {"due": at(days=1)},
            ),
            (
                "phi",
                _q(
                    "short",
                    "What does a phi-accrual failure detector output?",
                    "A suspicion level that grows with silence, not a boolean.",
                    topic="failure-detection",
                ),
                {"due": at(days=3), "step": 2, "last_review": at(days=-4)},
            ),
        ],
    )
    decks.set_pinned(user, b, True)
    _pin_pinned_at(b, at(days=-1))

    e = decks.create(user, "scratch", context_prompt=None, display_name="Scratch")

    t = decks.create_trivia(
        user,
        "world-history",
        topic="World history from antiquity to 1900.",
        interval_minutes=1440,
        display_name="World History Trivia",
    )
    qrepo = QuestionRepo()
    tq = TriviaQueueRepo()
    t_ids: dict[str, int] = {}
    for key, prompt, answer, regex, explanation in [
        (
            "rome",
            "Which empire's western half fell in 476?",
            "The Roman Empire",
            "(?i)rom",
            "Odoacer deposed Romulus Augustulus in 476.",
        ),
        (
            "print",
            "Who introduced movable-type printing to Europe around 1450?",
            "Johannes Gutenberg",
            "(?i)gutenberg",
            "The Gutenberg Bible followed in the mid 1450s.",
        ),
        (
            "magna",
            "In which year was Magna Carta sealed?",
            "1215",
            "1215",
            "At Runnymede, by King John.",
        ),
    ]:
        qid = qrepo.add(
            user,
            t,
            _q("short", prompt, answer, answer_regex=regex, explanation=explanation),
        )
        tq.append_card(qid, t)
        t_ids[key] = qid
    tq.mark_answered(t_ids["rome"], True)

    active = sessions.create(user, a, DEVICE_LABEL)
    _pin_session(active, last_active=at(minutes=-20), created_at=at(minutes=-25))
    snoozed = sessions.create(user, b, DEVICE_LABEL)
    _pin_session(snoozed, last_active=at(hours=-6), created_at=at(hours=-6, minutes=-10))
    sessions.snooze(user, snoozed, at(hours=3))

    notes = NotificationLogRepo()
    n1 = notes.append(
        user_id=user,
        title="3 cards due in World Capitals",
        body="Canberra, Nairobi and two more are waiting.",
        url="/study/world-capitals",
        source="digest",
    )
    _pin_notification(n1, at(hours=-3))
    n2 = notes.append(
        user_id=user,
        title="Distributed Systems is ready",
        body="One card came due while you were away.",
        url="/study/distributed-systems",
        source="when-ready",
    )
    _pin_notification(n2, at(days=-1))

    token = _insert_api_token(
        user,
        plaintext="prep_pat_ParityCliToken0000000000000000000000",
        label="Parity CLI",
        created_at=at(days=-3),
    )

    wid = "transform-world-capitals-parity01"
    ActiveWorkflowsRepo().register(
        workflow_id=wid,
        user_login=user,
        workflow_type=WorkflowType.TRANSFORM,
        deck_id=a,
        deck_name="world-capitals",
        url_path=f"/transform/{wid}",
        initial_status="computing",
    )
    _pin_workflow_started(wid, at(minutes=-5))

    return {
        "decks": {
            "srs_a": {"id": a, "slug": "world-capitals", "display": "World Capitals"},
            "srs_b": {"id": b, "slug": "distributed-systems", "display": "Distributed Systems"},
            "empty": {"id": e, "slug": "scratch", "display": "Scratch"},
            "trivia": {"id": t, "slug": "world-history", "display": "World History Trivia"},
        },
        "questions": {"srs_a": a_ids, "srs_b": b_ids, "trivia": t_ids},
        "sessions": {"active": active, "snoozed": snoozed},
        "notifications": [n1, n2],
        "api_tokens": [token],
        "workflows": {"transform": wid},
    }


def profile_study(user: str) -> dict:
    """One deck with every card type due, the mcq first, and a session
    one answer in (a warm-up mcq already reviewed). Dues sit in distinct
    wall-clock hours: the queue shuffles ties within an hour."""
    decks = DeckRepo()
    sessions = SessionRepo()
    d = decks.create(
        user,
        "geography",
        context_prompt="Physical and political geography.",
        display_name="Geography",
    )
    cards = _capitals_cards()
    cards = [c for c in cards if c[0] != "suspended"]
    pins = {
        "mcq": at(hours=-5),
        "short_regex": at(hours=-4),
        "multi": at(hours=-3),
        "code": at(hours=-2),
        "short_plain": at(hours=-1),
    }
    for key, _new, p in cards:
        p["due"] = pins[key]
        p.pop("step", None)
        p.pop("last_review", None)
    cards.append(
        (
            "warmup",
            _q(
                "mcq",
                "Which continent is Egypt in?",
                "Africa",
                choices=["Africa", "Asia", "Europe"],
                topic="geography",
            ),
            {"due": at(days=1), "step": 1, "last_review": at(minutes=-4)},
        )
    )
    ids = _insert_cards(user, d, cards)
    sid = sessions.create(user, d, DEVICE_LABEL)
    _pin_session(sid, last_active=at(minutes=-2), created_at=at(minutes=-6))
    _answer_in_session(sid, ids["warmup"], answered_at=at(minutes=-4), result="right")
    _add_review(ids["warmup"], ts=at(minutes=-4), result="right", user_answer="Africa")
    return {
        "deck": {"id": d, "slug": "geography", "display": "Geography"},
        "questions": ids,
        "session_id": sid,
    }


def profile_workflows(user: str) -> dict:
    """Two SRS decks and a trivia deck for the phase-4 job flows, plus a
    study session whose first due card is free text (the AI grader's
    entry point). Every job is started for real against Temporal by the
    flow, so nothing here is a workflow row; this is only the data the
    four job kinds act on."""
    decks = DeckRepo()
    sessions = SessionRepo()

    a = decks.create(
        user,
        "algorithms",
        context_prompt="Sorting, searching and complexity analysis.",
        display_name="Algorithms",
    )
    a_ids = _insert_cards(
        user,
        a,
        [
            (
                "complexity",
                _q(
                    "short",
                    "What is the average-case time complexity of quicksort?",
                    "O(n log n)",
                    topic="complexity",
                ),
                {"due": at(hours=-5)},
            ),
            (
                "traversal",
                _q(
                    "mcq",
                    "Which traversal visits a graph level by level?",
                    "Breadth-first search",
                    choices=["Depth-first search", "Breadth-first search", "Topological sort"],
                    topic="graphs",
                ),
                {"due": at(hours=-4)},
            ),
            (
                "binary_search",
                _q(
                    "code",
                    "Return the index of `target` in the sorted list `xs`, or -1.",
                    "def find(xs, target):\n    lo, hi = 0, len(xs) - 1\n"
                    "    while lo <= hi:\n        mid = (lo + hi) // 2\n"
                    "        if xs[mid] == target:\n            return mid\n"
                    "        if xs[mid] < target:\n            lo = mid + 1\n"
                    "        else:\n            hi = mid - 1\n    return -1\n",
                    language="python",
                    skeleton="def find(xs, target):\n    ...\n",
                    rubric="- Halves the range each step\n- Returns -1 on a miss",
                    topic="searching",
                ),
                {"due": at(hours=-3), "step": 2, "last_review": at(days=-3)},
            ),
            (
                "annotated",
                _q(
                    "short",
                    "Which sort is stable: heapsort or merge sort?",
                    "Merge sort",
                    answer_regex="(?i)merge",
                    explanation="Merge sort keeps equal keys in input order; heapsort does not.",
                    topic="sorting",
                ),
                {"due": at(days=2), "step": 3, "last_review": at(days=-6)},
            ),
            (
                "retired",
                _q(
                    "short",
                    "Which sort did the 1959 Shell paper describe?",
                    "Shellsort",
                    topic="history",
                ),
                {"due": at(days=4), "step": 1, "last_review": at(days=-8)},
            ),
            (
                "duplicate",
                _q(
                    "short",
                    "What is the average-case cost of quicksort?",
                    "O(n log n)",
                    topic="complexity",
                ),
                {"due": at(days=7)},
            ),
        ],
    )

    b = decks.create(
        user,
        "databases",
        context_prompt="Storage engines, indexes and transactions.",
        display_name="Databases",
    )
    b_ids = _insert_cards(
        user,
        b,
        [
            (
                "acid",
                _q(
                    "short",
                    "What does the I in ACID guarantee?",
                    "Concurrent transactions do not observe each other's partial writes.",
                    topic="transactions",
                ),
                {"due": at(days=1)},
            ),
            (
                "btree",
                _q(
                    "mcq",
                    "Which index shape keeps range scans sequential on disk?",
                    "B-tree",
                    choices=["Hash index", "B-tree", "Bloom filter"],
                    topic="indexes",
                ),
                {"due": at(days=3), "step": 1, "last_review": at(days=-2)},
            ),
            (
                "wal",
                _q(
                    "short",
                    "Why does a write-ahead log make crash recovery possible?",
                    "The log records an intent before the page changes, so recovery replays it.",
                    topic="durability",
                ),
                {"due": at(days=6), "step": 2, "last_review": at(days=-7)},
            ),
        ],
    )

    t = decks.create_trivia(
        user,
        "systems-trivia",
        topic="Operating systems and computer architecture.",
        interval_minutes=1440,
        display_name="Systems Trivia",
    )

    sid = sessions.create(user, a, DEVICE_LABEL)
    _pin_session(sid, last_active=at(minutes=-2), created_at=at(minutes=-6))

    return {
        "decks": {
            "srs_a": {"id": a, "slug": "algorithms", "display": "Algorithms"},
            "srs_b": {"id": b, "slug": "databases", "display": "Databases"},
            "trivia": {"id": t, "slug": "systems-trivia", "display": "Systems Trivia"},
        },
        "questions": {"srs_a": a_ids, "srs_b": b_ids},
        "session_id": sid,
    }


def profile_io(user: str) -> dict:
    """The import, export and split screens.

    One SRS deck whose cards carry every field an export writes, and one
    trivia deck, because the export hub is the only page that renders
    differently for the two. `import-*` starts from a deck name that does
    not exist, so nothing here reserves one.
    """
    decks = DeckRepo()
    qrepo = QuestionRepo()

    srs = decks.create(
        user,
        "algorithms",
        context_prompt="Sorting, searching and complexity.",
        display_name="Algorithms",
    )
    srs_ids = _insert_cards(
        user,
        srs,
        [
            (
                "complexity",
                _q(
                    "mcq",
                    "What is the average-case time of quicksort?",
                    "O(n log n)",
                    choices=["O(n)", "O(n log n)", "O(n^2)", "O(log n)"],
                    topic="complexity",
                ),
                {"due": at(hours=-2), "step": 2, "last_review": at(days=-2)},
            ),
            (
                "stability",
                _q(
                    "short",
                    "Name a comparison sort that is stable.",
                    "Merge sort",
                    answer_regex="(?i)merge",
                    topic="sorting",
                ),
                {"due": at(hours=-1), "step": 1, "last_review": at(days=-1)},
            ),
            (
                "binary",
                _q(
                    "code",
                    "Return the index of `needle` in the sorted list `xs`, or -1.",
                    "def find(xs, needle):\n    lo, hi = 0, len(xs) - 1\n    while lo <= hi:\n        mid = (lo + hi) // 2\n        if xs[mid] == needle:\n            return mid\n        if xs[mid] < needle:\n            lo = mid + 1\n        else:\n            hi = mid - 1\n    return -1\n",
                    language="python",
                    skeleton="def find(xs, needle):\n    ...\n",
                    rubric="- Halves the range each step\n- Returns -1 on a miss",
                    topic="searching",
                ),
                {"due": at(days=2), "step": 3, "last_review": at(days=-5)},
            ),
            (
                "invariant",
                _q(
                    "short",
                    "What does a loop invariant have to hold at?",
                    "Before the loop, after every iteration, and after the loop.",
                    topic="proofs",
                ),
                {"due": at(days=4), "step": 4, "last_review": at(days=-8)},
            ),
        ],
    )
    _add_review(srs_ids["complexity"], ts=at(days=-2), result="right", user_answer="O(n log n)")
    _add_review(srs_ids["stability"], ts=at(days=-1), result="wrong", user_answer="Heap sort")

    trivia = decks.create_trivia(
        user,
        "database-trivia",
        topic="Storage engines, transactions and query planning.",
        interval_minutes=1440,
        display_name="Database Trivia",
    )
    tq = TriviaQueueRepo()
    trivia_ids: dict[str, int] = {}
    for key, prompt, answer, regex in [
        (
            "isolation",
            "Which isolation level allows phantom reads?",
            "Repeatable read",
            "(?i)repeatable",
        ),
        (
            "index",
            "What structure does a clustered index store the rows in?",
            "The index itself",
            "(?i)index",
        ),
        (
            "wal",
            "What does a write-ahead log let a database skip on commit?",
            "Flushing the data pages",
            "(?i)flush",
        ),
    ]:
        qid = qrepo.add(user, trivia, _q("short", prompt, answer, answer_regex=regex))
        tq.append_card(qid, trivia)
        trivia_ids[key] = qid
    tq.mark_answered(trivia_ids["isolation"], True)

    return {
        "decks": {
            "srs": {"id": srs, "slug": "algorithms", "display": "Algorithms"},
            "trivia": {"id": trivia, "slug": "database-trivia", "display": "Database Trivia"},
        },
        "questions": {"srs": srs_ids, "trivia": trivia_ids},
    }


def _not_yet(name: str) -> Callable[[str], dict]:
    def build(user: str) -> dict:
        raise HTTPException(400, f"profile {name!r} is not implemented yet")

    return build


PROFILES: dict[str, Callable[[str], dict]] = {
    "empty": profile_empty,
    "reader": profile_reader,
    "study": profile_study,
    "workflows": profile_workflows,
    "io": profile_io,
    "caps": _not_yet("caps"),
}


def seed(user: str, profile: str) -> dict:
    build = PROFILES.get(profile)
    if build is None:
        raise HTTPException(400, f"unknown profile {profile!r} (have: {sorted(PROFILES)})")
    wipe_user(user)
    create_user(user)
    ids = build(user)
    return {"user": user, "profile": profile, "now": clock.now_iso(), **ids}


# ---- routes ----------------------------------------------------------------


def register(app: FastAPI) -> None:
    """Mount the seed route once."""
    if any(getattr(r, "path", None) == SEED_PATH for r in app.routes):
        return

    @app.post(SEED_PATH, include_in_schema=False)
    def parity_seed(body: SeedRequest, _gate: None = Depends(_require_internal_token)):
        return json.loads(json.dumps(seed(body.user, body.profile)))
