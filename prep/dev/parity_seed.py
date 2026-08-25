"""Parity seed profiles (docs/PARITY-GATE.md C6).

`POST /_parity/seed` wipes one login's rows, recreates the user in the
parity timezone and inserts a named profile through the repositories,
so the same call works on every target and survives migrations.
Timestamps that decide what a page shows are absolute from
`PARITY_NOW`; `created_at`-style stamps come from the process clock.

Mounted by `register(app)`, which the parity target launcher calls;
never mount it in a deploy.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from prep.agent.routes import _require_internal_token
from prep.auth.merge import discover_user_scoped_tables
from prep.auth.repo import UserRepo
from prep.decks.entities import NewQuestion, QuestionType
from prep.decks.repo import DeckRepo, QuestionRepo
from prep.infrastructure.db import cursor
from prep.notify.repo import NotificationLogRepo
from prep.study.repo import SessionRepo
from prep.trivia.repo import TriviaQueueRepo
from prep.workflows.entities import WorkflowType
from prep.workflows.repo import ActiveWorkflowsRepo

PARITY_NOW = datetime(2026, 3, 14, 15, 0, 0, tzinfo=timezone.utc)
PARITY_TZ = "America/New_York"
PARITY_DISPLAY_NAME = "Parity"
DEVICE_LABEL = "iPhone"

SEED_PATH = "/_parity/seed"


def at(**delta) -> str:
    """`PARITY_NOW` shifted by `timedelta(**delta)`, in the column format
    `db.now()` writes."""
    return (PARITY_NOW + timedelta(**delta)).isoformat()


class SeedRequest(BaseModel):
    user: str
    profile: str


# ---- wipe + user ----------------------------------------------------------


def wipe_user(user: str) -> None:
    with cursor() as c:
        for table, columns in discover_user_scoped_tables(c).items():
            for column in columns:
                c.execute(f'DELETE FROM "{table}" WHERE "{column}" = ?', (user,))
        c.execute("DELETE FROM users WHERE tailscale_login = ?", (user,))


def create_user(user: str) -> None:
    repo = UserRepo()
    repo.upsert(user, display_name=PARITY_DISPLAY_NAME)
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
            "trivia": {"id": t, "slug": "world-history", "display": "World History Trivia"},
        },
        "questions": {"srs_a": a_ids, "srs_b": b_ids, "trivia": t_ids},
        "sessions": {"active": active, "snoozed": snoozed},
        "notifications": [n1, n2],
        "workflows": {"transform": wid},
    }


def profile_study(user: str) -> dict:
    """One deck with every card type due, the mcq first, and a session
    one answer in."""
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
        "mcq": at(hours=-3),
        "short_regex": at(hours=-2),
        "multi": at(hours=-1),
        "code": at(minutes=-30),
        "short_plain": at(minutes=-10),
    }
    for key, _new, p in cards:
        p["due"] = pins[key]
        p.pop("step", None)
        p.pop("last_review", None)
    ids = _insert_cards(user, d, cards)
    sid = sessions.create(user, d, DEVICE_LABEL)
    _pin_session(sid, last_active=at(minutes=-2), created_at=at(minutes=-6))
    _answer_in_session(sid, ids["short_plain"], answered_at=at(minutes=-4), result="right")
    _add_review(ids["short_plain"], ts=at(minutes=-4), result="right", user_answer="Lima")
    return {
        "deck": {"id": d, "slug": "geography", "display": "Geography"},
        "questions": ids,
        "session_id": sid,
    }


def _not_yet(name: str) -> Callable[[str], dict]:
    def build(user: str) -> dict:
        raise HTTPException(400, f"profile {name!r} is not implemented yet")

    return build


PROFILES: dict[str, Callable[[str], dict]] = {
    "empty": profile_empty,
    "reader": profile_reader,
    "study": profile_study,
    "workflows": _not_yet("workflows"),
    "caps": _not_yet("caps"),
}


def seed(user: str, profile: str) -> dict:
    build = PROFILES.get(profile)
    if build is None:
        raise HTTPException(400, f"unknown profile {profile!r} (have: {sorted(PROFILES)})")
    wipe_user(user)
    create_user(user)
    ids = build(user)
    return {"user": user, "profile": profile, "now": PARITY_NOW.isoformat(), **ids}


# ---- routes ----------------------------------------------------------------


def register(app: FastAPI) -> None:
    """Mount the seed route once."""
    if any(getattr(r, "path", None) == SEED_PATH for r in app.routes):
        return

    @app.post(SEED_PATH, include_in_schema=False)
    def parity_seed(body: SeedRequest, _gate: None = Depends(_require_internal_token)):
        return json.loads(json.dumps(seed(body.user, body.profile)))
