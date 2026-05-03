"""Tests for prep.trivia.scheduler.

Exercise the per-deck dispatch logic with a real sqlite seeded via
the existing repos. send_to_user is monkey-patched so we can assert
on its inputs without touching pywebpush.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from prep.decks.entities import NewQuestion, QuestionType
from prep.decks.repo import DeckRepo, QuestionRepo
from prep.trivia import scheduler as sched
from prep.trivia.repo import TriviaQueueRepo


@pytest.fixture
def fixtures(initialized_db: str):
    user = initialized_db
    decks = DeckRepo()
    questions = QuestionRepo()
    trivia = TriviaQueueRepo()
    return {
        "user": user,
        "decks": decks,
        "questions": questions,
        "trivia": trivia,
    }


def _make_trivia_deck(fixtures, name="capitals", interval=30, n_questions=2):
    deck_id = fixtures["decks"].create_trivia(
        fixtures["user"], name, topic=f"{name} topic", interval_minutes=interval
    )
    qids = []
    for i in range(n_questions):
        qid = fixtures["questions"].add(
            fixtures["user"],
            deck_id,
            NewQuestion(type=QuestionType.SHORT, prompt=f"Q{i}?", answer=f"A{i}", topic=name),
        )
        fixtures["trivia"].append_card(qid, deck_id)
        qids.append(qid)
    return deck_id, qids


# ---- _is_due ------------------------------------------------------------


def test_is_due_when_never_notified():
    now = datetime.now(timezone.utc)
    assert sched._is_due(now, None, 30) is True
    assert sched._is_due(now, "", 30) is True


def test_is_due_after_interval_elapsed():
    now = datetime.now(timezone.utc)
    last = (now - timedelta(minutes=45)).isoformat()
    assert sched._is_due(now, last, 30) is True


def test_is_due_false_when_within_interval():
    now = datetime.now(timezone.utc)
    last = (now - timedelta(minutes=15)).isoformat()
    assert sched._is_due(now, last, 30) is False


def test_is_due_garbage_treated_as_never():
    now = datetime.now(timezone.utc)
    assert sched._is_due(now, "not-a-date", 30) is True


# ---- tick ---------------------------------------------------------------


def test_tick_sends_push_for_due_deck(monkeypatch, fixtures):
    deck_id, qids = _make_trivia_deck(fixtures, n_questions=2)
    sent = []

    def fake_send(*, user_id, title, body, url=None):
        sent.append({"user_id": user_id, "title": title, "body": body, "url": url})
        return {"ok": True}

    monkeypatch.setattr("prep.notify._legacy_module.send_to_user", fake_send)
    sched.tick(datetime.now(timezone.utc))
    assert len(sent) == 1
    assert sent[0]["body"] == "Q0?"  # first card in queue
    assert sent[0]["url"] == f"/trivia/{qids[0]}"
    assert sent[0]["title"] == "capitals"


def test_tick_skips_deck_within_interval(monkeypatch, fixtures):
    deck_id, _ = _make_trivia_deck(fixtures, interval=60)
    # Stamp last_notified_at to "5 minutes ago" — well within the 60min interval.
    fixtures["decks"].set_last_notified_at(
        deck_id, (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    )
    sent = []
    monkeypatch.setattr(
        "prep.notify._legacy_module.send_to_user",
        lambda **kw: sent.append(kw),
    )
    sched.tick(datetime.now(timezone.utc))
    assert sent == []


def test_tick_skips_when_queue_empty_and_agent_down(monkeypatch, fixtures):
    """Empty deck + agent unreachable → skip this tick, don't crash,
    don't update last_notified_at (so we'll retry next tick)."""
    fixtures["decks"].create_trivia(
        fixtures["user"], "empty", topic="something", interval_minutes=30
    )

    def boom(*_a, **_kw):
        from prep.trivia.agent_client import AgentUnavailable

        raise AgentUnavailable("agent down for tests")

    monkeypatch.setattr("prep.trivia.service.generate_batch", boom)
    sent = []
    monkeypatch.setattr(
        "prep.notify._legacy_module.send_to_user",
        lambda **kw: sent.append(kw),
    )
    sched.tick(datetime.now(timezone.utc))
    assert sent == []


def test_tick_updates_last_notified_at(monkeypatch, fixtures):
    deck_id, _ = _make_trivia_deck(fixtures)
    monkeypatch.setattr(
        "prep.notify._legacy_module.send_to_user",
        lambda **kw: {"ok": True},
    )
    sched.tick(datetime.now(timezone.utc))
    rows = fixtures["decks"].list_trivia_decks()
    row = next(r for r in rows if r["id"] == deck_id)
    assert row["last_notified_at"] is not None


def test_tick_skips_deck_with_notifications_disabled(monkeypatch, fixtures):
    """The per-deck pause toggle should silence the scheduler without
    touching last_notified_at (so resuming doesn't immediately fire)."""
    deck_id, _ = _make_trivia_deck(fixtures, name="muted")
    fixtures["decks"].set_notifications_enabled(fixtures["user"], deck_id, False)
    sent = []
    monkeypatch.setattr(
        "prep.notify._legacy_module.send_to_user",
        lambda **kw: sent.append(kw),
    )
    sched.tick(datetime.now(timezone.utc))
    assert sent == []
    rows = fixtures["decks"].list_trivia_decks()
    row = next(r for r in rows if r["id"] == deck_id)
    assert row["last_notified_at"] is None
