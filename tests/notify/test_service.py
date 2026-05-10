"""Service-level tests for the notify bounded context.

The notify package's "service" surface is split across two modules:
- `prep.notify.push` — VAPID + per-user fanout (`send_to_user`,
  `subscribe`).
- `prep.notify.scheduler` — periodic tick loop (existing async
  regression test in `tests/test_notify_scheduler_loop.py`).

This file covers the push side: subscribe validation, fanout
counting, and stale-subscription pruning. The actual webpush() call
is stubbed; we're testing the orchestration around it, not pywebpush
itself.
"""

from __future__ import annotations

import importlib

import pytest

from prep.notify.repo import NotificationLogRepo, PushSubsRepo


@pytest.fixture
def push_module(initialized_db: str, monkeypatch):
    """Reload `prep.notify.push` against the per-test DB. Returns the
    freshly-imported module so tests can monkeypatch its `_send_one`
    + `_load_or_create_keys` boundary without leaking across tests."""
    from prep.notify import push as _push_mod

    importlib.reload(_push_mod)
    # Stub VAPID key access so tests don't write keys to disk.
    monkeypatch.setattr(
        _push_mod, "_load_or_create_keys", lambda: {"vapid": object(), "public_b64": "stub"}
    )
    return _push_mod


def test_subscribe_persists_and_round_trips(push_module, initialized_db: str):
    """subscribe(...) reads endpoint/keys.p256dh/keys.auth out of the
    browser-shaped payload and upserts via PushSubsRepo."""
    push_module.subscribe(
        initialized_db,
        {
            "endpoint": "https://push.example/abc",
            "keys": {"p256dh": "pk", "auth": "ak"},
        },
    )
    subs = PushSubsRepo().list_for_user_raw(initialized_db)
    assert len(subs) == 1
    assert subs[0]["endpoint"] == "https://push.example/abc"


def test_subscribe_rejects_incomplete_payload(push_module, initialized_db: str):
    """Missing endpoint or keys → ValueError; nothing persisted."""
    import pytest as _pytest

    with _pytest.raises(ValueError):
        push_module.subscribe(
            initialized_db,
            {"endpoint": "https://push.example/abc"},  # no `keys`
        )
    assert PushSubsRepo().count_for_user(initialized_db) == 0


def test_send_to_user_counts_results_and_logs(push_module, initialized_db: str, monkeypatch):
    """send_to_user fans out to every subscribed device, counts
    sent/failed/pruned, and writes a row to the notification log
    regardless of delivery success."""
    PushSubsRepo().upsert(initialized_db, "https://ok", "p", "a")
    PushSubsRepo().upsert(initialized_db, "https://gone", "p", "a")
    PushSubsRepo().upsert(initialized_db, "https://fail", "p", "a")

    def stub_send(sub_row, payload):
        if sub_row["endpoint"] == "https://ok":
            return "ok"
        if sub_row["endpoint"] == "https://gone":
            return "gone"
        return "fail"

    monkeypatch.setattr(push_module, "_send_one", stub_send)

    result = push_module.send_to_user(
        initialized_db,
        "title",
        "body",
        url="/notify",
        source="manual",
    )
    assert result == {"sent": 1, "failed": 1, "pruned": 1}
    # The 'gone' row was pruned.
    remaining = {s["endpoint"] for s in PushSubsRepo().list_for_user_raw(initialized_db)}
    assert remaining == {"https://ok", "https://fail"}
    # One log row regardless of delivery outcome.
    log = NotificationLogRepo().list_recent(initialized_db, limit=10)
    assert len(log) == 1
    assert log[0].title == "title"
    assert log[0].source == "manual"


def test_send_to_user_prepends_root_path(push_module, initialized_db: str, monkeypatch):
    """ROOT_PATH gets prepended so the SW's notificationclick handler
    lands inside PWA scope instead of bouncing to the start_url."""
    monkeypatch.setenv("ROOT_PATH", "/prep")
    captured: dict = {}

    def stub_send(_sub, payload):
        captured.update(payload)
        return "ok"

    monkeypatch.setattr(push_module, "_send_one", stub_send)
    PushSubsRepo().upsert(initialized_db, "https://ok", "p", "a")

    push_module.send_to_user(initialized_db, "t", "b", url="/notify")
    assert captured["url"] == "/prep/notify"


def test_send_to_user_no_subs_yields_zero(push_module, initialized_db: str):
    """User with zero devices: counts are all zero, nothing persisted
    on the push side; log row still appended."""
    result = push_module.send_to_user(initialized_db, "t", "b")
    assert result == {"sent": 0, "failed": 0, "pruned": 0}
    log = NotificationLogRepo().list_recent(initialized_db, limit=10)
    assert len(log) == 1
