"""Route tests for the JSON study API (prep/study/api.py).

Drives the router through FastAPI's TestClient: HTTP request → router
→ service → repo → sqlite. Covers the CardSource-shaped contract the
browser components consume (next / submit / author / advance /
abandon / snooze / draft) plus the invariants that must hold on every
endpoint: auth required, cross-user ids answer 404, and a stale
session version is a distinct, actionable conflict.
"""

from __future__ import annotations

import types

import pytest
from fastapi.testclient import TestClient

from prep.decks.entities import NewQuestion, QuestionType
from prep.decks.repo import DeckRepo, QuestionRepo
from prep.study.entities import SessionState
from prep.study.repo import SessionRepo

OTHER_USER = "bob@example.com"


# ---- fixtures + helpers -------------------------------------------------


def _seed_deck(user: str, name: str = "study-api") -> tuple[int, int]:
    """An SRS deck with one mcq card. Returns (deck_id, qid)."""
    deck_id = DeckRepo().create(user, name)
    qid = QuestionRepo().add(
        user,
        deck_id,
        NewQuestion(
            type=QuestionType.MCQ,
            prompt="2+2?",
            answer="4",
            choices=["3", "4", "5"],
        ),
    )
    return deck_id, qid


def _seed_short(user: str, deck_id: int, prompt: str = "Define quorum.") -> int:
    return QuestionRepo().add(
        user,
        deck_id,
        NewQuestion(
            type=QuestionType.SHORT,
            prompt=prompt,
            answer="A majority of replicas.",
            rubric="Mentions majority.",
        ),
    )


def _seed_other_user() -> tuple[int, int]:
    """A second user with their own deck + card, for the IDOR checks."""
    from prep.auth.repo import UserRepo

    UserRepo().upsert(OTHER_USER, display_name="Bob")
    deck_id = DeckRepo().create(OTHER_USER, "bobs-deck")
    qid = QuestionRepo().add(
        OTHER_USER,
        deck_id,
        NewQuestion(type=QuestionType.SHORT, prompt="bob's card", answer="secret"),
    )
    return deck_id, qid


def _begin(client: TestClient, name: str = "study-api", **body) -> dict:
    r = client.post(f"/api/study/decks/{name}/session", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _afake(value):
    """Async fake: coroutine factory that ignores args and resolves to
    `value`."""

    async def fn(*_a, **_kw):
        return value

    return fn


@pytest.fixture
def no_agent():
    """Pin the deploy to 'no AI grader' so free-text submits take the
    self-grade branch deterministically."""
    from prep import agent as agent_mod

    class _Unavailable:
        available = False

    agent_mod.set_agent(_Unavailable())
    yield
    agent_mod.set_agent(None)


@pytest.fixture
def with_agent(monkeypatch):
    """Pin the deploy to 'AI grader configured' so free-text submits
    take the Temporal path: an adapter to run, and a tier that funds
    the workflow start."""
    from prep import agent as agent_mod
    from prep.agent.fake import FakeAgent

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test-not-real")
    agent_mod.set_agent(FakeAgent())
    yield
    agent_mod.set_agent(None)


# ---- next: the card payload --------------------------------------------


def test_begin_returns_first_card(client: TestClient, initialized_db: str):
    _seed_deck(initialized_db)
    body = _begin(client)
    assert body["card"]["prompt"] == "2+2?"
    assert body["session"]["state"] == "awaiting-answer"
    assert body["session"]["version"] == 1


def test_begin_resumes_the_open_session(client: TestClient, initialized_db: str):
    _seed_deck(initialized_db)
    first = _begin(client)
    again = _begin(client)
    assert again["session"]["id"] == first["session"]["id"]


def test_begin_fresh_starts_a_new_session(client: TestClient, initialized_db: str):
    _seed_deck(initialized_db)
    first = _begin(client)
    fresh = _begin(client, fresh=True)
    assert fresh["session"]["id"] != first["session"]["id"]
    old = SessionRepo().get(initialized_db, first["session"]["id"])
    assert old is not None and old.status.value == "abandoned"


def test_begin_refuses_trivia_deck(client: TestClient, initialized_db: str):
    DeckRepo().create_trivia(initialized_db, "trivia-deck", topic="things", interval_minutes=30)
    r = client.post("/api/study/decks/trivia-deck/session", json={})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "not_studiable"


def test_card_payload_matches_the_component_field_names(client: TestClient, initialized_db: str):
    """The components read question_id / type / prompt / choices /
    skeleton off the card, and never see the canonical answer before
    they submit."""
    _seed_deck(initialized_db)
    card = _begin(client)["card"]
    assert set(card) == {
        "question_id",
        "deck_id",
        "type",
        "prompt",
        "choices",
        "skeleton",
        "language",
    }
    assert card["type"] == "mcq"
    assert card["choices"] == ["3", "4", "5"]


def test_session_next_reads_the_current_view(client: TestClient, initialized_db: str):
    _seed_deck(initialized_db)
    sid = _begin(client)["session"]["id"]
    r = client.get(f"/api/study/sessions/{sid}/next")
    assert r.status_code == 200
    assert r.json()["card"]["prompt"] == "2+2?"


def test_caught_up_on_an_empty_deck(client: TestClient, initialized_db: str):
    """No cards at all: caught up, with nothing scheduled ahead."""
    DeckRepo().create(initialized_db, "empty-deck")
    body = _begin(client, name="empty-deck")
    assert body["caughtUp"] == {"nextDueMinutes": None}
    assert body["session"]["status"] == "completed"


def test_caught_up_reports_minutes_until_the_next_card(client: TestClient, initialized_db: str):
    """After the only card is answered it is rescheduled into the
    future, so the caught-up screen gets a positive minute count."""
    _, qid = _seed_deck(initialized_db)
    started = _begin(client)
    sid = started["session"]["id"]
    graded = client.post(
        f"/api/study/sessions/{sid}/submit",
        json={"question_id": qid, "version": started["session"]["version"], "answer": "4"},
    ).json()
    advanced = client.post(
        f"/api/study/sessions/{sid}/advance",
        json={"version": graded["session"]["version"]},
    )
    assert advanced.status_code == 200
    summary = advanced.json()["caughtUp"]
    assert isinstance(summary["nextDueMinutes"], int)
    assert summary["nextDueMinutes"] >= 1


# ---- submit: the three outcomes ----------------------------------------


def test_submit_mcq_returns_the_verdict_inline(client: TestClient, initialized_db: str):
    _, qid = _seed_deck(initialized_db)
    started = _begin(client)
    sid = started["session"]["id"]
    r = client.post(
        f"/api/study/sessions/{sid}/submit",
        json={"question_id": qid, "version": started["session"]["version"], "answer": "4"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "right"
    assert body["idk"] is False
    assert isinstance(body["nextDueMinutes"], int)
    # The verdict view needs the canonical answer; it arrives now, not
    # with the question.
    assert body["card"]["answer"] == "4"
    assert body["session"]["state"] == "showing-result"


def test_submit_wrong_mcq(client: TestClient, initialized_db: str):
    _, qid = _seed_deck(initialized_db)
    started = _begin(client)
    r = client.post(
        f"/api/study/sessions/{started['session']['id']}/submit",
        json={"question_id": qid, "version": started["session"]["version"], "answer": "5"},
    )
    assert r.json()["verdict"] == "wrong"


def test_submit_idk_records_wrong_with_a_blank_answer(client: TestClient, initialized_db: str):
    _, qid = _seed_deck(initialized_db)
    started = _begin(client)
    r = client.post(
        f"/api/study/sessions/{started['session']['id']}/submit",
        json={"question_id": qid, "version": started["session"]["version"], "idk": True},
    )
    body = r.json()
    assert body["verdict"] == "wrong"
    assert body["idk"] is True
    assert body["answer"] == ""


def test_submit_free_text_without_an_agent_asks_for_a_self_grade(
    no_agent, client: TestClient, initialized_db: str
):
    deck_id, _ = _seed_deck(initialized_db)
    qid = _seed_short(initialized_db, deck_id)
    started = _begin(client)
    r = client.post(
        f"/api/study/sessions/{started['session']['id']}/submit",
        json={"question_id": qid, "version": started["session"]["version"], "answer": "most of em"},
    )
    body = r.json()
    assert body["selfGrade"] is True
    # The reveal renders the canonical answer + rubric against what the
    # user wrote.
    assert body["card"]["answer"] == "A majority of replicas."
    assert body["card"]["rubric"] == "Mentions majority."
    assert body["answer"] == "most of em"


def test_self_grade_verdict_records_the_review(no_agent, client: TestClient, initialized_db: str):
    deck_id, _ = _seed_deck(initialized_db)
    qid = _seed_short(initialized_db, deck_id)
    started = _begin(client)
    sid = started["session"]["id"]
    r = client.post(
        f"/api/study/sessions/{sid}/submit",
        json={
            "question_id": qid,
            "version": started["session"]["version"],
            "verdict": "right",
            "answer": "a majority",
        },
    )
    body = r.json()
    assert body["verdict"] == "right"
    assert isinstance(body["nextDueMinutes"], int)
    s = SessionRepo().get(initialized_db, sid)
    assert s is not None and s.state.value == "showing-result"


def test_self_grade_rejects_an_unknown_verdict(no_agent, client: TestClient, initialized_db: str):
    deck_id, _ = _seed_deck(initialized_db)
    qid = _seed_short(initialized_db, deck_id)
    started = _begin(client)
    r = client.post(
        f"/api/study/sessions/{started['session']['id']}/submit",
        json={"question_id": qid, "version": started["session"]["version"], "verdict": "maybe"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_verdict"


def test_submit_free_text_with_an_agent_returns_a_poll_url(
    with_agent, monkeypatch, client: TestClient, initialized_db: str
):
    """The async path hands back {pending: {poll}} and parks the
    session in `grading` for the poll to reconcile."""
    from prep import temporal_client

    deck_id, _ = _seed_deck(initialized_db)
    qid = _seed_short(initialized_db, deck_id)
    started = _begin(client)
    sid = started["session"]["id"]
    wid = f"grade-study-api-q{qid}-abc1234567"
    monkeypatch.setattr(
        temporal_client,
        "start_grading",
        _afake(types.SimpleNamespace(workflow_id=wid, run_id="run-1")),
    )

    r = client.post(
        f"/api/study/sessions/{sid}/submit",
        json={"question_id": qid, "version": started["session"]["version"], "answer": "a majority"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["pending"]["workflow_id"] == wid
    assert body["pending"]["poll"].endswith(f"/api/study/grading/{wid}?sid={sid}")
    assert body["session"]["state"] == "grading"


def test_submit_stale_version_is_a_conflict(client: TestClient, initialized_db: str):
    _, qid = _seed_deck(initialized_db)
    started = _begin(client)
    r = client.post(
        f"/api/study/sessions/{started['session']['id']}/submit",
        json={"question_id": qid, "version": 999, "answer": "4"},
    )
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "stale_version"
    assert err["current_version"] == started["session"]["version"]


def test_session_submit_without_a_version_is_rejected(client: TestClient, initialized_db: str):
    _, qid = _seed_deck(initialized_db)
    started = _begin(client)
    r = client.post(
        f"/api/study/sessions/{started['session']['id']}/submit",
        json={"question_id": qid, "answer": "4"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "version_required"


# ---- grading polling ----------------------------------------------------


def test_grading_poll_stays_pending_while_the_workflow_runs(
    monkeypatch, client: TestClient, initialized_db: str
):
    from prep import temporal_client

    deck_id, _ = _seed_deck(initialized_db)
    qid = _seed_short(initialized_db, deck_id)
    wid = f"grade-study-api-q{qid}-abc1234567"
    monkeypatch.setattr(temporal_client, "get_grade_progress", _afake({"status": "grading"}))
    monkeypatch.setattr(temporal_client, "describe_workflow", _afake({"status": "RUNNING"}))

    r = client.get(f"/api/study/grading/{wid}")
    assert r.status_code == 200
    body = r.json()
    assert body["pending"]["workflow_id"] == wid
    assert body["pending"]["status"] == "grading"
    assert "verdict" not in body


def test_grading_poll_lands_the_verdict_and_reconciles_the_session(
    monkeypatch, client: TestClient, initialized_db: str
):
    from prep import temporal_client

    deck_id, _ = _seed_deck(initialized_db)
    qid = _seed_short(initialized_db, deck_id)
    started = _begin(client)
    sid = started["session"]["id"]
    wid = f"grade-study-api-q{qid}-abc1234567"
    SessionRepo().set_grading(initialized_db, sid, qid, wid, started["session"]["version"])

    monkeypatch.setattr(temporal_client, "get_grade_progress", _afake({"status": "done"}))
    monkeypatch.setattr(temporal_client, "describe_workflow", _afake({"status": "COMPLETED"}))
    monkeypatch.setattr(
        temporal_client,
        "get_grade_result",
        _afake(
            {
                "verdict": {"result": "right", "feedback": "Good."},
                "state": {
                    "step": 1,
                    "next_due": "2030-01-01T00:00:00+00:00",
                    "interval_minutes": 1440,
                },
                "user_answer": "a majority",
                "idk": False,
            }
        ),
    )

    r = client.get(f"/api/study/grading/{wid}?sid={sid}")
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "right"
    assert body["nextDueMinutes"] == 1440
    assert body["answer"] == "a majority"
    assert body["card"]["answer"] == "A majority of replicas."
    assert body["session"]["state"] == "showing-result"


def test_grading_poll_reports_a_workflow_that_produced_nothing(
    monkeypatch, client: TestClient, initialized_db: str
):
    from prep import temporal_client

    deck_id, _ = _seed_deck(initialized_db)
    qid = _seed_short(initialized_db, deck_id)
    wid = f"grade-study-api-q{qid}-abc1234567"
    monkeypatch.setattr(
        temporal_client,
        "get_grade_progress",
        _afake({"status": "failed", "error": "grader blew up"}),
    )
    monkeypatch.setattr(temporal_client, "describe_workflow", _afake({"status": "FAILED"}))
    monkeypatch.setattr(temporal_client, "get_grade_result", _afake(None))

    r = client.get(f"/api/study/grading/{wid}")
    assert r.status_code == 200
    assert r.json()["failed"]["code"] == "grading_failed"


def test_grading_poll_rejects_a_malformed_workflow_id(client: TestClient, initialized_db: str):
    r = client.get("/api/study/grading/not-a-grade-id")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "malformed_workflow_id"


# ---- session lifecycle --------------------------------------------------


def test_advance_returns_the_next_card(client: TestClient, initialized_db: str):
    deck_id, qid = _seed_deck(initialized_db)
    second = QuestionRepo().add(
        initialized_db,
        deck_id,
        NewQuestion(type=QuestionType.MCQ, prompt="3+3?", answer="6", choices=["5", "6"]),
    )
    started = _begin(client)
    sid = started["session"]["id"]
    first_qid = started["card"]["question_id"]
    graded = client.post(
        f"/api/study/sessions/{sid}/submit",
        json={
            "question_id": first_qid,
            "version": started["session"]["version"],
            "answer": "nope",
        },
    ).json()
    r = client.post(
        f"/api/study/sessions/{sid}/advance", json={"version": graded["session"]["version"]}
    )
    assert r.status_code == 200
    assert r.json()["card"]["question_id"] in {qid, second}
    assert r.json()["card"]["question_id"] != first_qid


def test_advance_stale_version_is_a_conflict(client: TestClient, initialized_db: str):
    _seed_deck(initialized_db)
    sid = _begin(client)["session"]["id"]
    r = client.post(f"/api/study/sessions/{sid}/advance", json={"version": 999})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_version"


def test_draft_autosave_bumps_the_version(client: TestClient, initialized_db: str):
    _seed_deck(initialized_db)
    started = _begin(client)
    sid = started["session"]["id"]
    r = client.post(
        f"/api/study/sessions/{sid}/draft",
        json={"version": started["session"]["version"], "draft": "half an answer"},
    )
    assert r.status_code == 200
    assert r.json()["version"] == started["session"]["version"] + 1
    s = SessionRepo().get(initialized_db, sid)
    assert s is not None and s.current_draft == "half an answer"


def test_draft_stale_version_is_a_conflict(client: TestClient, initialized_db: str):
    _seed_deck(initialized_db)
    sid = _begin(client)["session"]["id"]
    r = client.post(f"/api/study/sessions/{sid}/draft", json={"version": 999, "draft": "x"})
    assert r.status_code == 409
    assert r.json()["error"]["current_version"] == 1


def test_abandon_ends_the_session(client: TestClient, initialized_db: str):
    _seed_deck(initialized_db)
    sid = _begin(client)["session"]["id"]
    r = client.post(f"/api/study/sessions/{sid}/abandon")
    assert r.status_code == 200
    assert r.json()["ended"]["reason"] == "abandoned"
    s = SessionRepo().get(initialized_db, sid)
    assert s is not None and s.status.value == "abandoned"


def test_snooze_sets_and_clears_the_wake_time(client: TestClient, initialized_db: str):
    _seed_deck(initialized_db)
    sid = _begin(client)["session"]["id"]
    r = client.post(f"/api/study/sessions/{sid}/snooze", json={"preset": "1h"})
    assert r.status_code == 200
    assert r.json()["snoozed_until"]
    woken = client.post(f"/api/study/sessions/{sid}/snooze", json={"preset": "wake"})
    assert woken.json()["snoozed_until"] is None


def test_snooze_rejects_an_unknown_preset(client: TestClient, initialized_db: str):
    _seed_deck(initialized_db)
    sid = _begin(client)["session"]["id"]
    r = client.post(f"/api/study/sessions/{sid}/snooze", json={"preset": "someday"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_duration"


# ---- deck-scoped study (no session) ------------------------------------


def test_deck_next_serves_a_due_card(client: TestClient, initialized_db: str):
    _seed_deck(initialized_db)
    r = client.get("/api/study/decks/study-api/next")
    assert r.status_code == 200
    body = r.json()
    assert body["card"]["prompt"] == "2+2?"
    assert body["session"] is None


def test_deck_next_404s_for_an_unknown_deck(client: TestClient, initialized_db: str):
    r = client.get("/api/study/decks/no-such-deck/next")
    assert r.status_code == 404
    assert DeckRepo().find_id(initialized_db, "no-such-deck") is None


def test_deck_submit_records_without_a_session(client: TestClient, initialized_db: str):
    _, qid = _seed_deck(initialized_db)
    r = client.post("/api/study/decks/study-api/submit", json={"question_id": qid, "answer": "4"})
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "right"
    assert body["session"] is None


# ---- authoring ----------------------------------------------------------


def test_author_writes_a_card_into_the_named_deck(client: TestClient, initialized_db: str):
    deck_id, _ = _seed_deck(initialized_db)
    r = client.post(
        "/api/study/cards",
        json={"prompt": "What is a quorum?", "answer": "A majority.", "deck_id": deck_id},
    )
    assert r.status_code == 201
    card = r.json()["card"]
    assert card["type"] == "short"
    assert card["answer"] == "A majority."
    assert card["deck_id"] == deck_id
    # Due immediately, like the online manual add.
    assert client.get("/api/study/decks/study-api/next").status_code == 200


def test_author_without_a_deck_files_into_the_inbox(client: TestClient, initialized_db: str):
    r = client.post("/api/study/cards", json={"prompt": "Front", "answer": "Back"})
    assert r.status_code == 201
    inbox = DeckRepo().find_id(initialized_db, "inbox")
    assert inbox is not None
    assert r.json()["card"]["deck_id"] == inbox


def test_author_requires_both_sides(client: TestClient, initialized_db: str):
    r = client.post("/api/study/cards", json={"prompt": "  ", "answer": "Back"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_card"


def test_author_refuses_another_users_deck(client: TestClient, initialized_db: str):
    other_deck, _ = _seed_other_user()
    r = client.post(
        "/api/study/cards",
        json={"prompt": "Front", "answer": "Back", "deck_id": other_deck},
    )
    assert r.status_code == 404


# ---- IDOR: another user's ids are indistinguishable from missing --------


def test_session_endpoints_404_on_another_users_session(client: TestClient, initialized_db: str):
    other_deck, other_qid = _seed_other_user()
    other_sid = SessionRepo().create(OTHER_USER, other_deck, "laptop")

    assert client.get(f"/api/study/sessions/{other_sid}/next").status_code == 404
    assert (
        client.post(
            f"/api/study/sessions/{other_sid}/submit",
            json={"question_id": other_qid, "version": 1, "answer": "secret"},
        ).status_code
        == 404
    )
    assert (
        client.post(f"/api/study/sessions/{other_sid}/advance", json={"version": 1}).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/study/sessions/{other_sid}/draft", json={"version": 1, "draft": "x"}
        ).status_code
        == 404
    )
    assert client.post(f"/api/study/sessions/{other_sid}/abandon").status_code == 404
    assert (
        client.post(f"/api/study/sessions/{other_sid}/snooze", json={"preset": "1h"}).status_code
        == 404
    )
    # Nothing moved on bob's row.
    s = SessionRepo().get(OTHER_USER, other_sid)
    assert s is not None and s.status.value == "active" and s.version == 1


def test_session_submit_404s_on_another_users_question(client: TestClient, initialized_db: str):
    _seed_deck(initialized_db)
    _, other_qid = _seed_other_user()
    started = _begin(client)
    r = client.post(
        f"/api/study/sessions/{started['session']['id']}/submit",
        json={"question_id": other_qid, "version": started["session"]["version"], "answer": "x"},
    )
    assert r.status_code == 404


def test_deck_endpoints_404_on_another_users_deck(client: TestClient, initialized_db: str):
    _, other_qid = _seed_other_user()
    assert client.get("/api/study/decks/bobs-deck/next").status_code == 404
    assert (
        client.post(
            "/api/study/decks/bobs-deck/submit", json={"question_id": other_qid, "answer": "x"}
        ).status_code
        == 404
    )


def test_grading_poll_404s_on_another_users_workflow(
    monkeypatch, client: TestClient, initialized_db: str
):
    """The wid embeds the question id, so a guessed wid must not expose
    another user's grade. The ownership check fires before Temporal."""
    from prep import temporal_client

    _, other_qid = _seed_other_user()
    monkeypatch.setattr(temporal_client, "get_grade_progress", _afake({"status": "done"}))

    r = client.get(f"/api/study/grading/grade-bobs-deck-q{other_qid}-abc1234567")
    assert r.status_code == 404


# ---- auth ---------------------------------------------------------------


def test_every_endpoint_requires_an_identity(env: None, monkeypatch):
    """Without a resolvable user the whole surface refuses. Mirrors the
    app-wide invariant pinned in tests/test_smoke.py."""
    monkeypatch.delenv("PREP_DEFAULT_USER", raising=False)

    import importlib

    from prep.infrastructure import db as db_mod

    importlib.reload(db_mod)

    from prep import app as app_mod

    importlib.reload(app_mod)

    c = TestClient(app_mod.app)
    calls = [
        ("post", "/api/study/decks/d/session", {}),
        ("get", "/api/study/decks/d/next", None),
        ("post", "/api/study/decks/d/submit", {"question_id": 1, "answer": "x"}),
        ("get", "/api/study/sessions/s/next", None),
        ("post", "/api/study/sessions/s/submit", {"question_id": 1, "version": 1}),
        ("post", "/api/study/sessions/s/advance", {"version": 1}),
        ("post", "/api/study/sessions/s/draft", {"version": 1, "draft": ""}),
        ("post", "/api/study/sessions/s/abandon", None),
        ("post", "/api/study/sessions/s/snooze", {"preset": "1h"}),
        ("post", "/api/study/cards", {"prompt": "a", "answer": "b"}),
        ("get", "/api/study/grading/grade-d-q1-abc1234567", None),
    ]
    for method, path, body in calls:
        kwargs = {"json": body} if body is not None else {}
        r = getattr(c, method)(path, follow_redirects=False, **kwargs)
        assert r.status_code == 401, f"{method.upper()} {path} → {r.status_code}"


def test_failed_grading_releases_the_session_instead_of_wedging_it(
    monkeypatch, client: TestClient, initialized_db: str
):
    """A workflow that ends with no verdict must not leave the session
    in 'grading'. While it did, every later read answered {pending} for
    a dead workflow and the client polled it forever."""
    from prep import temporal_client

    deck_id, _ = _seed_deck(initialized_db)
    qid = _seed_short(initialized_db, deck_id)
    started = _begin(client)
    sid = started["session"]["id"]
    wid = f"grade-study-api-q{qid}-abc1234567"
    SessionRepo().set_grading(initialized_db, sid, qid, wid, started["session"]["version"])

    monkeypatch.setattr(
        temporal_client, "get_grade_progress", _afake({"status": "failed", "error": "boom"})
    )
    monkeypatch.setattr(temporal_client, "describe_workflow", _afake({"status": "FAILED"}))
    monkeypatch.setattr(temporal_client, "get_grade_result", _afake(None))

    r = client.get(f"/api/study/grading/{wid}?sid={sid}")
    assert r.status_code == 200
    assert r.json()["failed"]["code"] == "grading_failed"

    s = SessionRepo().get(initialized_db, sid)
    assert s is not None
    assert s.state is not SessionState.GRADING
    assert s.current_grading_workflow_id is None

    # And the next read hands back a card, not another pending screen.
    nxt = client.get(f"/api/study/sessions/{sid}/next")
    assert nxt.status_code == 200
    assert "pending" not in nxt.json()


def test_failed_grading_poll_cannot_clear_a_newer_workflow(
    monkeypatch, client: TestClient, initialized_db: str
):
    """A late poll for an old workflow must not release a session that
    has since started grading something else."""
    from prep import temporal_client

    deck_id, _ = _seed_deck(initialized_db)
    qid = _seed_short(initialized_db, deck_id)
    started = _begin(client)
    sid = started["session"]["id"]
    stale_wid = f"grade-study-api-q{qid}-stale111111"
    current_wid = f"grade-study-api-q{qid}-current1111"
    SessionRepo().set_grading(initialized_db, sid, qid, current_wid, started["session"]["version"])

    monkeypatch.setattr(
        temporal_client, "get_grade_progress", _afake({"status": "failed", "error": "boom"})
    )
    monkeypatch.setattr(temporal_client, "describe_workflow", _afake({"status": "FAILED"}))
    monkeypatch.setattr(temporal_client, "get_grade_result", _afake(None))

    client.get(f"/api/study/grading/{stale_wid}?sid={sid}")

    s = SessionRepo().get(initialized_db, sid)
    assert s is not None
    assert s.state is SessionState.GRADING
    assert s.current_grading_workflow_id == current_wid
