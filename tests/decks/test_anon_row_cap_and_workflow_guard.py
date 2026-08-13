"""Two bounds on an anonymous account: the rows it can write, and the
workflows it can start.

The row cap sits in the repos, so every write path inherits it. The
workflow guard sits at the start call sites, because a start books a
worker slot before the activity discovers there is no adapter.
"""

from __future__ import annotations

import pytest

from prep.auth import limits
from prep.auth.limits import RowCapReached
from prep.auth.repo import UserRepo
from prep.decks.entities import NewQuestion, QuestionType
from prep.decks.repo import DeckRepo, QuestionRepo
from prep.infrastructure.db import cursor
from tests.anon_support import ANON_ID, seed_anon_user

NAMED_ID = "named@example.com"


def a_card(prompt: str = "2+2?") -> NewQuestion:
    return NewQuestion(type=QuestionType.SHORT, prompt=prompt, answer="4")


def counts(user_id: str) -> tuple[int, int]:
    with cursor() as c:
        decks = c.execute(
            "SELECT COUNT(*) AS n FROM decks WHERE user_id = ?", (user_id,)
        ).fetchone()["n"]
        questions = c.execute(
            "SELECT COUNT(*) AS n FROM questions WHERE user_id = ?", (user_id,)
        ).fetchone()["n"]
    return decks, questions


# ---- the row cap --------------------------------------------------------


def test_an_anonymous_account_is_refused_the_sixth_deck(initialized_db: str, monkeypatch):
    monkeypatch.setattr(limits, "ANON_MAX_DECKS", 2)
    uid = seed_anon_user()
    repo = DeckRepo()
    repo.create(uid, "one")
    repo.create(uid, "two")
    with pytest.raises(RowCapReached):
        repo.create(uid, "three")
    assert counts(uid) == (2, 0)


def test_the_cap_covers_trivia_decks_too(initialized_db: str, monkeypatch):
    monkeypatch.setattr(limits, "ANON_MAX_DECKS", 1)
    uid = seed_anon_user()
    repo = DeckRepo()
    repo.create(uid, "one")
    with pytest.raises(RowCapReached):
        repo.create_trivia(uid, "two", topic="things", interval_minutes=30)
    assert counts(uid) == (1, 0)


def test_an_anonymous_account_is_refused_the_next_question(initialized_db: str, monkeypatch):
    monkeypatch.setattr(limits, "ANON_MAX_QUESTIONS", 2)
    uid = seed_anon_user()
    deck_id = DeckRepo().create(uid, "deck")
    q_repo = QuestionRepo()
    q_repo.add(uid, deck_id, a_card("one"))
    q_repo.add(uid, deck_id, a_card("two"))
    with pytest.raises(RowCapReached):
        q_repo.add(uid, deck_id, a_card("three"))
    assert counts(uid) == (1, 2)


def test_get_or_create_is_capped_but_still_resolves_owned_decks(initialized_db: str, monkeypatch):
    """`get_or_create` is a second insert path, and it sits on GETs:
    uncapped, `/deck/<any name>` mints a deck per request. Resolving a
    deck that already exists must survive the cap, or an account over
    its ceiling is locked out of decks it owns."""
    monkeypatch.setattr(limits, "ANON_MAX_DECKS", 1)
    uid = seed_anon_user()
    repo = DeckRepo()
    first = repo.get_or_create(uid, "one")
    with pytest.raises(RowCapReached):
        repo.get_or_create(uid, "two")
    assert repo.get_or_create(uid, "one") == first
    assert counts(uid) == (1, 0)


def test_a_deck_page_get_mints_nothing_past_the_cap(anon_client, monkeypatch):
    monkeypatch.setattr(limits, "ANON_MAX_DECKS", 1)
    DeckRepo().create(ANON_ID, "one")
    for i in range(4):
        assert anon_client.get(f"/deck/zzz{i}").status_code == 429
    assert counts(ANON_ID) == (1, 0)


def test_concurrent_creates_cannot_overshoot_the_cap(initialized_db: str, monkeypatch):
    """The count and the insert it gates must sit in one write
    transaction. A count taken outside it lets N racing requests all
    read cap-1 and all insert."""
    import threading

    monkeypatch.setattr(limits, "ANON_MAX_DECKS", 5)
    uid = seed_anon_user()
    repo = DeckRepo()
    for i in range(4):
        repo.create(uid, f"seed{i}")

    start = threading.Barrier(6)

    def attempt(n: int):
        start.wait()
        try:
            DeckRepo().create(uid, f"racer{n}")
        except RowCapReached:
            pass

    threads = [threading.Thread(target=attempt, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert counts(uid)[0] == 5


def test_a_named_account_is_capped_by_neither(initialized_db: str, monkeypatch):
    monkeypatch.setattr(limits, "ANON_MAX_DECKS", 1)
    monkeypatch.setattr(limits, "ANON_MAX_QUESTIONS", 1)
    UserRepo().upsert(NAMED_ID, display_name="Named")
    repo = DeckRepo()
    deck_id = repo.create(NAMED_ID, "one")
    repo.create(NAMED_ID, "two")
    QuestionRepo().add(NAMED_ID, deck_id, a_card("one"))
    QuestionRepo().add(NAMED_ID, deck_id, a_card("two"))
    assert counts(NAMED_ID) == (2, 2)


def test_a_merged_account_is_capped_by_neither(initialized_db: str, monkeypatch):
    """The cap follows `is_anonymous`, so clearing the flag (what a
    merge target never carries) lifts it with no other change."""
    monkeypatch.setattr(limits, "ANON_MAX_DECKS", 1)
    uid = seed_anon_user()
    DeckRepo().create(uid, "one")
    with cursor() as c:
        c.execute("UPDATE users SET is_anonymous = 0 WHERE tailscale_login = ?", (uid,))
    DeckRepo().create(uid, "two")
    assert counts(uid) == (2, 0)


def test_the_refusal_reaches_a_browser_as_a_limit_page(anon_client, monkeypatch):
    monkeypatch.setattr(limits, "ANON_MAX_DECKS", 1)
    DeckRepo().create(ANON_ID, "one")
    r = anon_client.post(
        "/decks/new/srs",
        data={"name": "two", "action": "empty"},
        follow_redirects=False,
    )
    assert r.status_code == 429
    assert "limit reached" in r.text


# ---- the workflow guard -------------------------------------------------


def active_workflow_rows() -> int:
    with cursor() as c:
        return c.execute("SELECT COUNT(*) AS n FROM active_workflows").fetchone()["n"]


def test_a_plan_post_by_an_anonymous_account_starts_no_workflow(anon_client, monkeypatch):
    from prep import temporal_client as tc

    started = False

    async def fake_start(**kwargs):
        nonlocal started
        started = True
        raise AssertionError("workflow must not start")

    monkeypatch.setattr(tc, "start_plan_generate", fake_start)
    # Force the availability pre-check open so the funding guard is
    # what refuses, not the template-level flag in front of it.
    monkeypatch.setattr("prep.agent.is_available", True, raising=False)
    r = anon_client.post(
        "/decks/new/srs",
        data={"name": "planned", "action": "plan", "context_prompt": "Go channels"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert started is False
    assert active_workflow_rows() == 0


def test_a_trivia_post_by_an_anonymous_account_starts_no_workflow(anon_client, monkeypatch):
    from prep import temporal_client as tc

    started = False

    async def fake_start(**kwargs):
        nonlocal started
        started = True
        raise AssertionError("workflow must not start")

    monkeypatch.setattr(tc, "start_trivia_generate", fake_start)
    monkeypatch.setattr("prep.agent.is_available", True, raising=False)
    r = anon_client.post(
        "/decks/new/trivia",
        data={"name": "quiz", "topic": "world capitals", "notification_interval_minutes": "30"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/trivia/gen/" not in r.headers["location"]
    assert started is False
    assert active_workflow_rows() == 0


def test_a_reorganize_post_by_an_anonymous_account_starts_no_workflow(anon_client, monkeypatch):
    from prep import temporal_client as tc

    started = False

    async def fake_start(**kwargs):
        nonlocal started
        started = True
        raise AssertionError("workflow must not start")

    monkeypatch.setattr(tc, "start_transform", fake_start)
    monkeypatch.setattr("prep.agent.is_available", True, raising=False)
    r = anon_client.post(
        "/reorganize",
        data={"prompt": "split my decks by topic"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert started is False
    assert active_workflow_rows() == 0
    # The refusal lands in the form's own error slot, not on an error page.
    assert "error-banner" in r.text
    assert 'name="prompt"' in r.text


def test_an_empty_deck_still_lands_for_an_anonymous_account(anon_client):
    r = anon_client.post(
        "/decks/new/srs",
        data={"name": "manual", "action": "empty"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert active_workflow_rows() == 0
    assert counts(ANON_ID)[0] == 1


def test_a_named_user_with_no_funding_is_refused_identically(initialized_db: str, monkeypatch):
    """The guard is funding, not anonymity: it closes the same hole
    for a signed-in user with no key."""
    from prep.agent.port import AgentUnavailable
    from prep.decks import service

    class _Client:
        async def start_plan_generate(self, **kwargs):
            raise AssertionError("workflow must not start")

    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    with pytest.raises(AgentUnavailable):
        import asyncio

        asyncio.run(
            service.start_plan_generation(
                _Client(),
                user_id=initialized_db,
                deck_id=1,
                deck_name="deck",
                prompt="prompt",
            )
        )
    assert active_workflow_rows() == 0


def test_the_refusal_reaches_a_json_client_in_its_own_error_envelope(anon_client, monkeypatch):
    """The study client reads `error.code` / `error.message`; any other
    shape reaches the user as an unlabeled "request failed (429)",
    dropping the one message that tells them what to do."""
    monkeypatch.setattr(limits, "ANON_MAX_QUESTIONS", 0)
    deck_id = DeckRepo().create(ANON_ID, "deck")
    r = anon_client.post(
        "/api/study/cards",
        json={"deck_id": deck_id, "prompt": "front", "answer": "back"},
        headers={"accept": "application/json"},
    )
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "deck_limit"
    assert "Create an account" in r.json()["error"]["message"]
    assert counts(ANON_ID) == (1, 0)
