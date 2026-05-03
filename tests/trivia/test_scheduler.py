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

    def fake_send(*, user_id, title, body, url=None, source="manual", tag=None):
        sent.append(
            {
                "user_id": user_id,
                "title": title,
                "body": body,
                "url": url,
                "source": source,
                "tag": tag,
            }
        )
        return {"ok": True}

    monkeypatch.setattr("prep.notify._legacy_module.send_to_user", fake_send)
    sched.tick(datetime.now(timezone.utc))
    assert len(sent) == 1
    assert sent[0]["body"] == "Q0?"  # first card in queue
    # Deep link → session route, not single-card route — tapping the
    # push opens a 3-card mini-session.
    assert sent[0]["url"] == "/trivia/session/capitals"
    assert sent[0]["title"] == "capitals"
    # Per-deck tag so iOS coalesces stacked pushes for this deck.
    assert sent[0]["tag"] == "trivia-capitals"


def test_tick_skips_deck_within_interval(monkeypatch, fixtures):
    deck_id, _ = _make_trivia_deck(fixtures, interval=60)
    # Stamp last_notified_at to "5 minutes ago" — well within the 60min interval.
    fixtures["decks"].record_notification_fire(
        deck_id, (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(), 0
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


def test_tick_does_not_refill_when_pending_pool_full(monkeypatch, fixtures):
    """Fresh deck of 25 has pending_review=25 → no refill should fire,
    even though the scheduler is processing this deck on a tick."""
    deck_id, _ = _make_trivia_deck(fixtures, n_questions=25)
    gen_calls = []

    def fake_gen(**kwargs):
        gen_calls.append(kwargs)
        return type("Outcome", (), {"inserted": 0, "skipped_duplicates": 0, "skipped_invalid": 0})()

    monkeypatch.setattr("prep.trivia.service.generate_batch", fake_gen)
    monkeypatch.setattr("prep.notify._legacy_module.send_to_user", lambda **kw: {"ok": True})
    sched.tick(datetime.now(timezone.utc))
    assert gen_calls == []


def test_tick_refills_when_pending_pool_drained(monkeypatch, fixtures):
    """Once the user has worked through the pool (no never-shown, no
    wrong cards), the scheduler proactively refills."""
    deck_id, qids = _make_trivia_deck(fixtures, n_questions=4)
    # Mark all 4 right → pending_review=0 → below threshold.
    for qid in qids:
        fixtures["trivia"].mark_answered(qid, correct=True)
    gen_calls = []

    def fake_gen(**kwargs):
        gen_calls.append(kwargs)
        return type("Outcome", (), {"inserted": 0, "skipped_duplicates": 0, "skipped_invalid": 0})()

    monkeypatch.setattr("prep.trivia.service.generate_batch", fake_gen)
    monkeypatch.setattr("prep.notify._legacy_module.send_to_user", lambda **kw: {"ok": True})
    sched.tick(datetime.now(timezone.utc))
    assert len(gen_calls) == 1
    assert gen_calls[0]["deck_id"] == deck_id


def test_tick_holds_refill_while_user_has_wrong_cards(monkeypatch, fixtures):
    """Wrong-answered cards count as 'pending'. If the user has 5+
    wrong-or-fresh, no refill — they need to clear what they have."""
    deck_id, qids = _make_trivia_deck(fixtures, n_questions=8)
    # Six wrong + 2 fresh → pending=8 → above threshold → no refill.
    for qid in qids[:6]:
        fixtures["trivia"].mark_answered(qid, correct=False)
    gen_calls = []
    monkeypatch.setattr(
        "prep.trivia.service.generate_batch",
        lambda **kw: gen_calls.append(kw) or type("O", (), {"inserted": 0})(),
    )
    monkeypatch.setattr("prep.notify._legacy_module.send_to_user", lambda **kw: {"ok": True})
    sched.tick(datetime.now(timezone.utc))
    assert gen_calls == []


def test_tick_skips_when_user_in_quiet_hours(monkeypatch, fixtures):
    """Quiet hours used to be SRS-only; now applies to trivia too.
    A trivia deck owned by a user in quiet hours should NOT fire,
    and last_notified_at should NOT advance (so it fires the moment
    the window reopens)."""
    from datetime import datetime as _dt
    from datetime import time as _time
    from datetime import timezone as _tz
    from zoneinfo import ZoneInfo

    deck_id, _ = _make_trivia_deck(fixtures, name="muted-by-quiet")

    # Set the user's prefs to quiet 22-8 in America/New_York. Pin
    # "now" to 03:00 NY (07:00 UTC) so we're squarely inside quiet.
    from prep import db as _db

    _db.set_notification_prefs(
        fixtures["user"],
        {
            "mode": "off",
            "tz": "America/New_York",
            "quiet_hours_enabled": True,
            "quiet_start_hour": 22,
            "quiet_end_hour": 8,
        },
    )
    quiet_now_local = _dt.combine(_dt.now(_tz.utc).date(), _time(3, 0)).replace(
        tzinfo=ZoneInfo("America/New_York")
    )
    quiet_now_utc = quiet_now_local.astimezone(_tz.utc)

    sent = []
    monkeypatch.setattr("prep.notify._legacy_module.send_to_user", lambda **kw: sent.append(kw))
    sched.tick(quiet_now_utc)
    assert sent == []
    rows = fixtures["decks"].list_trivia_decks()
    row = next(r for r in rows if r["id"] == deck_id)
    assert row["last_notified_at"] is None  # didn't advance


def test_tick_fires_outside_quiet_hours(monkeypatch, fixtures):
    """Same prefs, but local hour is 14:00 → outside quiet → should fire."""
    from datetime import datetime as _dt
    from datetime import time as _time
    from datetime import timezone as _tz
    from zoneinfo import ZoneInfo

    deck_id, _ = _make_trivia_deck(fixtures, name="active-window")
    from prep import db as _db

    _db.set_notification_prefs(
        fixtures["user"],
        {
            "mode": "off",
            "tz": "America/New_York",
            "quiet_hours_enabled": True,
            "quiet_start_hour": 22,
            "quiet_end_hour": 8,
        },
    )
    active_now_local = _dt.combine(_dt.now(_tz.utc).date(), _time(14, 0)).replace(
        tzinfo=ZoneInfo("America/New_York")
    )
    active_now_utc = active_now_local.astimezone(_tz.utc)

    sent = []
    monkeypatch.setattr(
        "prep.notify._legacy_module.send_to_user",
        lambda **kw: sent.append(kw) or {"ok": True},
    )
    sched.tick(active_now_utc)
    assert len(sent) == 1


# ---- exponential backoff -------------------------------------------------


def test_effective_interval_doubles_with_streak():
    f = sched._effective_interval_minutes
    assert f(30, 0) == 30
    assert f(30, 1) == 60
    assert f(30, 2) == 120
    assert f(30, 3) == 240
    # Capped at MAX_BACKOFF_DOUBLINGS (5 doublings = 32x).
    assert f(30, 5) == 30 * 32
    assert f(30, 9) == 30 * 32  # past cap stays at cap


def test_is_due_respects_backoff():
    """A deck whose previous fire was 45 minutes ago at base=30 IS
    due — but if the streak is 1 (effective=60min), it's NOT yet."""
    now = datetime.now(timezone.utc)
    last = (now - timedelta(minutes=45)).isoformat()
    assert sched._is_due(now, last, 30, ignored_streak=0) is True
    assert sched._is_due(now, last, 30, ignored_streak=1) is False  # need 60min
    assert sched._is_due(now, last, 30, ignored_streak=2) is False  # need 120min


def test_tick_increments_streak_when_no_engagement(monkeypatch, fixtures):
    """Two consecutive fires with no answer in between → streak goes
    from 0 → 1 after the first ignored fire."""
    deck_id, _ = _make_trivia_deck(fixtures, name="ignored", interval=30)
    # Pin "last fire" to 60 min ago so the deck is due even if the
    # streak bumps to 1 (which would require 60min wait next time).
    fixtures["decks"].record_notification_fire(
        deck_id, (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat(), 0
    )
    monkeypatch.setattr("prep.notify._legacy_module.send_to_user", lambda **kw: {"ok": True})
    sched.tick(datetime.now(timezone.utc))
    rows = fixtures["decks"].list_trivia_decks()
    row = next(r for r in rows if r["id"] == deck_id)
    # No answer happened between the simulated prior fire and this tick →
    # the prior fire counts as ignored → streak bumps to 1.
    assert row["notification_ignored_streak"] == 1


def test_tick_resets_streak_when_user_engaged(monkeypatch, fixtures):
    """If any card in the deck was answered after the previous fire,
    the next tick that lands on a due moment resets the streak to 0.
    Prior fire is far enough back that the backed-off interval has
    elapsed — otherwise the tick skips the deck entirely (cooldown)
    and never gets to the engagement check."""
    deck_id, qids = _make_trivia_deck(fixtures, name="re-engaged", interval=30)
    # Streak=1 → effective=60min. Prior fire 70 min ago → due NOW.
    prior = (datetime.now(timezone.utc) - timedelta(minutes=70)).isoformat()
    fixtures["decks"].record_notification_fire(deck_id, prior, 1)
    # User answered one card 5 min ago — engagement happened AFTER prior fire.
    # Direct UPDATE so we don't trigger mark_answered's own streak-reset
    # (we want to prove the SCHEDULER's engagement check works).
    from prep.infrastructure.db import cursor

    five_ago = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with cursor() as c:
        c.execute(
            "UPDATE trivia_queue SET last_answered_at = ?, last_answered_correctly = 1 WHERE question_id = ?",
            (five_ago, qids[0]),
        )
    monkeypatch.setattr("prep.notify._legacy_module.send_to_user", lambda **kw: {"ok": True})
    sched.tick(datetime.now(timezone.utc))
    rows = fixtures["decks"].list_trivia_decks()
    row = next(r for r in rows if r["id"] == deck_id)
    assert row["notification_ignored_streak"] == 0


def test_tick_holds_off_while_within_backed_off_interval(monkeypatch, fixtures):
    """Streak=2 (effective=120min on a 30min deck), last fire 45min ago
    → not yet due, no fire."""
    deck_id, _ = _make_trivia_deck(fixtures, name="cooldown", interval=30)
    fixtures["decks"].record_notification_fire(
        deck_id, (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(), 2
    )
    sent = []
    monkeypatch.setattr("prep.notify._legacy_module.send_to_user", lambda **kw: sent.append(kw))
    sched.tick(datetime.now(timezone.utc))
    assert sent == []


def test_mark_answered_immediately_resets_deck_streak(fixtures):
    """The answer-recording path resets the streak directly (not just
    on the next scheduler tick) so the next fire goes back to base
    interval the moment the user re-engages."""
    deck_id, qids = _make_trivia_deck(fixtures, name="bounceback", interval=30)
    # Manually set streak to a non-zero value.
    from prep.infrastructure.db import cursor

    with cursor() as c:
        c.execute("UPDATE decks SET notification_ignored_streak = 4 WHERE id = ?", (deck_id,))
    fixtures["trivia"].mark_answered(qids[0], correct=True)
    rows = fixtures["decks"].list_trivia_decks()
    row = next(r for r in rows if r["id"] == deck_id)
    assert row["notification_ignored_streak"] == 0


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
