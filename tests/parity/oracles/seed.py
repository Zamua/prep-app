"""Profiles the DB-backed oracles record against, written through the
repositories so they survive schema migrations.

`reader`: two SRS decks (one pinned, one with a suspended card), one
trivia deck, a snoozed session, unseen notifications, and an
awaiting-review workflow for the badge. Due cards sit in distinct
hour buckets so the study queue has no random tie to break.
"""

from __future__ import annotations

import json
from datetime import timedelta

from tests.parity.oracles import PARITY_NOW, PARITY_TZ, PARITY_USER, PARITY_USER_NAME

ANON_ID = "anon:" + "ab" * 16


def upsert_parity_user(login: str = PARITY_USER, name: str = PARITY_USER_NAME) -> dict:
    from prep.auth.repo import UserRepo
    from prep.notify.entities import NotificationPrefs
    from prep.notify.repo import NotifyPrefsRepo

    user = UserRepo().upsert(external_id=login, email=login, display_name=name)
    NotifyPrefsRepo().set(login, NotificationPrefs(tz=PARITY_TZ))
    return user


def _card(qtype: str, prompt: str, answer, **fields) -> "NewQuestion":  # noqa: F821
    from prep.decks.entities import NewQuestion, QuestionType

    return NewQuestion(type=QuestionType(qtype), prompt=prompt, answer=answer, **fields)


def _stagger_due(deck_id: int, minutes_ago: list[int | None]) -> None:
    """Distinct `next_due` per card, oldest first; None means not due."""
    from prep.infrastructure.db import cursor

    with cursor() as c:
        qids = [
            r["id"]
            for r in c.execute(
                "SELECT id FROM questions WHERE deck_id = ? ORDER BY id", (deck_id,)
            ).fetchall()
        ]
        for qid, ago in zip(qids, minutes_ago, strict=True):
            due = PARITY_NOW + timedelta(minutes=(-ago if ago is not None else 60 * 24 * 3))
            c.execute("UPDATE cards SET next_due = ? WHERE question_id = ?", (due.isoformat(), qid))


def seed_reader(login: str = PARITY_USER) -> dict:
    """Returns the ids the recorders need."""
    from prep.decks.repo import DeckRepo, QuestionRepo
    from prep.notify.repo import NotificationLogRepo
    from prep.study.repo import ReviewRepo, SessionRepo
    from prep.trivia.repo import TriviaQueueRepo
    from prep.workflows.entities import WorkflowType
    from prep.workflows.repo import ActiveWorkflowsRepo

    upsert_parity_user(login)
    decks, questions = DeckRepo(), QuestionRepo()

    capitals = decks.create(login, "capitals", "Every capital city.", "World Capitals")
    q_paris = questions.add(
        login,
        capitals,
        _card(
            "short",
            "Capital of **France**?",
            "Paris",
            topic="europe",
            answer_regex=r"paris",
            explanation="Paris has been the capital since 987.",
        ),
    )
    q_tokyo = questions.add(
        login,
        capitals,
        _card(
            "mcq",
            "Which city is the capital of Japan?",
            "Tokyo",
            topic="asia",
            choices=["Tokyo", "Osaka", "Kyoto", "Nagoya"],
        ),
    )
    q_andes = questions.add(
        login,
        capitals,
        _card(
            "multi",
            "Which of these are capitals?",
            json.dumps(["Lima", "Quito"]),
            topic="south-america",
            choices=["Lima", "Quito", "Cusco", "Guayaquil"],
        ),
    )
    q_add = questions.add(
        login,
        capitals,
        _card(
            "code",
            "Write `add(a, b)` returning the sum.",
            "def add(a, b):\n    return a + b",
            topic="python",
            skeleton="def add(a, b):\n    pass",
            language="python",
            rubric="- returns a + b",
        ),
    )
    q_suspended = questions.add(
        login, capitals, _card("short", "Capital of Atlantis?", "none", topic="myth")
    )
    questions.set_suspended(login, q_suspended, True)
    decks.set_pinned(login, capitals, True)

    reviews = ReviewRepo()
    reviews.record(login, q_paris, "right", "Paris")
    reviews.record(login, q_tokyo, "wrong", "Osaka")
    _stagger_due(capitals, [240, 180, 120, 60, None])

    distsys = decks.create(login, "distsys", None, "Distributed Systems")
    q_quorum = questions.add(
        login, distsys, _card("short", "Minimum quorum size for n=5?", "3", topic="consensus")
    )
    q_raft = questions.add(
        login,
        distsys,
        _card(
            "mcq",
            "Which Raft role appends entries?",
            "leader",
            choices=["leader", "follower", "candidate"],
        ),
    )
    _stagger_due(distsys, [90, None])

    trivia = decks.create_trivia(
        login,
        "history-trivia",
        topic="world history",
        interval_minutes=45,
        display_name="World History Trivia",
    )
    queue = TriviaQueueRepo()
    trivia_qids = []
    for prompt, answer, regex in (
        ("Who painted the Mona Lisa?", "Leonardo da Vinci", r"(leonardo )?da vinci"),
        ("Year the Berlin Wall fell?", "1989", r"1989"),
        ("First emperor of Rome?", "Augustus", r"augustus|octavian"),
    ):
        qid = questions.add(login, trivia, _card("short", prompt, answer, answer_regex=regex))
        queue.append_card(qid, trivia)
        trivia_qids.append(qid)
    queue.mark_answered(trivia_qids[0], True)

    sessions = SessionRepo()
    snoozed = sessions.create(login, distsys, "iPhone")
    sessions.snooze(login, snoozed, (PARITY_NOW + timedelta(days=7)).isoformat())

    log = NotificationLogRepo()
    log.append(
        user_id=login,
        title="3 cards due",
        body="World Capitals has 3 cards waiting.",
        url="/deck/capitals",
        source="srs-when-ready",
    )
    log.append(
        user_id=login,
        title="Trivia time",
        body="Who painted the Mona Lisa?",
        url="/trivia/session/history-trivia",
        source="trivia",
    )

    ActiveWorkflowsRepo().register(
        workflow_id="transform-distsys-PARITY01",
        user_login=login,
        workflow_type=WorkflowType.TRANSFORM,
        deck_id=distsys,
        deck_name="distsys",
        url_path="/transform/transform-distsys-PARITY01",
        initial_status="awaiting_apply",
    )

    return {
        "user": login,
        "decks": {"capitals": capitals, "distsys": distsys, "history-trivia": trivia},
        "questions": {
            "paris": q_paris,
            "tokyo": q_tokyo,
            "andes": q_andes,
            "add": q_add,
            "suspended": q_suspended,
            "quorum": q_quorum,
            "raft": q_raft,
            "trivia": trivia_qids,
        },
        "snoozed_session": snoozed,
    }


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
