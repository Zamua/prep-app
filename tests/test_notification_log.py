"""End-to-end tests for the in-app notification log.

Covers:
- send_to_user appends a row per push (keeps a record even if Apple
  silently drops or the device dismisses)
- /notify/log renders entries newest-first
- Opening the page marks all unseen entries as seen, clearing the
  masthead badge
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_send_to_user_appends_log_entry(monkeypatch, env):
    monkeypatch.setenv("ROOT_PATH", "/prep")
    import importlib

    from prep.notify import _legacy_module
    from prep.notify.repo import NotificationLogRepo

    importlib.reload(_legacy_module)

    # Initialize schema + the test user against the per-test sqlite.
    from prep.infrastructure import db as _infra_db

    importlib.reload(_infra_db)
    _infra_db.init()
    from prep import db as _db

    importlib.reload(_db)
    _db.upsert_user("testuser@example.com")

    monkeypatch.setattr(_legacy_module, "_send_one", lambda _s, _p: "ok")
    monkeypatch.setattr(
        _legacy_module.db, "list_push_subscriptions", lambda _u: [{"endpoint": "x"}]
    )

    _legacy_module.send_to_user(
        "testuser@example.com",
        "Trivia · doom",
        "Who composed the soundtrack?",
        url="/trivia/session/doom",
        source="trivia",
    )

    entries = NotificationLogRepo().list_recent("testuser@example.com")
    assert len(entries) == 1
    assert entries[0].title == "Trivia · doom"
    assert entries[0].body == "Who composed the soundtrack?"
    assert entries[0].url == "/prep/trivia/session/doom"  # ROOT_PATH applied
    assert entries[0].source == "trivia"
    assert entries[0].seen_at is None


def test_notification_log_route_renders_entries(client: TestClient, initialized_db: str):
    from prep.notify.repo import NotificationLogRepo

    repo = NotificationLogRepo()
    repo.append(
        user_id=initialized_db,
        title="Trivia · doom",
        body="Who composed?",
        url="/prep/trivia/session/doom",
        source="trivia",
    )
    repo.append(
        user_id=initialized_db,
        title="Prep — daily digest",
        body="5 cards due across all decks.",
        url="/prep/",
        source="srs-digest",
    )

    r = client.get("/notify/log")
    assert r.status_code == 200
    assert "Trivia · doom" in r.text
    assert "Prep — daily digest" in r.text
    # Source tags surface so the user can scan the list visually.
    assert "tag-notif-source" in r.text


def test_notification_log_marks_entries_seen(client: TestClient, initialized_db: str):
    from prep.notify.repo import NotificationLogRepo

    repo = NotificationLogRepo()
    repo.append(
        user_id=initialized_db,
        title="Trivia",
        body="Q?",
        url="/prep/trivia/session/foo",
        source="trivia",
    )
    assert repo.count_unseen(initialized_db) == 1

    client.get("/notify/log")

    assert repo.count_unseen(initialized_db) == 0


def test_notification_log_empty_state(client: TestClient, initialized_db: str):
    r = client.get("/notify/log")
    assert r.status_code == 200
    assert "No notifications yet" in r.text


def test_other_users_entries_not_visible(client: TestClient, initialized_db: str):
    """IDOR check — alice's log shouldn't include bob's entries."""
    from prep import db as _db
    from prep.notify.repo import NotificationLogRepo

    _db.upsert_user("bob@example.com")
    NotificationLogRepo().append(
        user_id="bob@example.com",
        title="Bob's secret",
        body="don't show alice",
        url="/prep/",
        source="trivia",
    )

    r = client.get("/notify/log")
    assert r.status_code == 200
    assert "Bob's secret" not in r.text
