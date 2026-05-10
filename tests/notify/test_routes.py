"""HTTP route tests for the notify bounded context.

Covers the user-facing surfaces:
- GET /notify renders the settings page
- POST /notify/prefs persists merged prefs
- POST /notify/subscribe registers a device
- POST /notify/unsubscribe drops by endpoint

The push send path is exercised separately in tests/test_notification_log.py
(needs more elaborate _send_one stubbing); this file stays focused on
the persistence + render edges.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from prep.notify.entities import NotifyMode
from prep.notify.repo import NotifyPrefsRepo, PushSubsRepo


def test_notify_settings_renders(client: TestClient, initialized_db: str):
    r = client.get("/notify")
    assert r.status_code == 200
    # Page mentions one of the user-facing copy strings.
    assert "notification" in r.text.lower() or "notify" in r.text.lower()


def test_prefs_save_round_trips(client: TestClient, initialized_db: str):
    """POST /notify/prefs persists, preserving scheduler-managed state
    (last_digest_date, last_when_ready_at) untouched."""
    r = client.post(
        "/notify/prefs",
        json={"mode": "digest", "digest_hour": 7, "threshold": 5},
    )
    assert r.status_code == 200
    prefs = NotifyPrefsRepo().get(initialized_db)
    assert prefs.mode == NotifyMode.DIGEST
    assert prefs.digest_hour == 7
    assert prefs.threshold == 5


def test_prefs_save_rejects_bad_value(client: TestClient, initialized_db: str):
    """Out-of-range values fail validation → 422."""
    r = client.post(
        "/notify/prefs",
        # digest_hour caps at 23
        json={"mode": "digest", "digest_hour": 99},
    )
    assert r.status_code == 422


def test_prefs_save_rejects_non_object(client: TestClient, initialized_db: str):
    r = client.post("/notify/prefs", json=["not", "an", "object"])
    assert r.status_code == 400


def test_subscribe_persists_endpoint(client: TestClient, initialized_db: str):
    payload = {
        "endpoint": "https://push.example/abc",
        "keys": {"p256dh": "pk", "auth": "ak"},
    }
    r = client.post("/notify/subscribe", json=payload)
    assert r.status_code == 200
    subs = PushSubsRepo().list_for_user_raw(initialized_db)
    assert len(subs) == 1
    assert subs[0]["endpoint"] == "https://push.example/abc"


def test_subscribe_rejects_missing_endpoint(client: TestClient, initialized_db: str):
    r = client.post("/notify/subscribe", json={"keys": {"p256dh": "pk", "auth": "ak"}})
    assert r.status_code == 400


def test_unsubscribe_drops_endpoint(client: TestClient, initialized_db: str):
    PushSubsRepo().upsert(initialized_db, "https://push/keep", "p", "a")
    PushSubsRepo().upsert(initialized_db, "https://push/drop", "p", "a")
    r = client.post("/notify/unsubscribe", json={"endpoint": "https://push/drop"})
    assert r.status_code == 200
    remaining = [s["endpoint"] for s in PushSubsRepo().list_for_user_raw(initialized_db)]
    assert remaining == ["https://push/keep"]
