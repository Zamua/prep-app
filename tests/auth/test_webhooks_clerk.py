"""Tests for the Clerk webhook receiver — signature verification +
user.{created,updated,deleted} → local users-table sync.

We don't want a real Svix signature dance in unit tests, so the
test patches `svix.webhooks.Webhook.verify` to either return the
JSON body or raise WebhookVerificationError. That keeps the focus
on our handler's behavior — the Svix lib itself has its own tests."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def webhook_client(env: None, monkeypatch):
    """Build a client with CLERK_WEBHOOK_SECRET set BEFORE importing
    app.py — the conditional include_router in app.py only mounts the
    Clerk webhook route when the env var is present at import time."""
    import importlib

    monkeypatch.setenv("CLERK_WEBHOOK_SECRET", "whsec_testfake")

    from prep.infrastructure import db as db_mod

    importlib.reload(db_mod)
    from prep import app as app_mod

    importlib.reload(app_mod)

    with TestClient(app_mod.app) as c:
        yield c


def _user_created_payload(user_id: str = "user_2abc", email: str = "alice@example.com") -> dict:
    """Realistic-shape Clerk user.created event body."""
    return {
        "type": "user.created",
        "data": {
            "id": user_id,
            "first_name": "Alice",
            "last_name": "Anderson",
            "image_url": "https://img/alice.png",
            "email_addresses": [
                {"id": "idn_1", "email_address": email},
            ],
            "primary_email_address_id": "idn_1",
        },
    }


def test_webhook_user_created_upserts_local_row(webhook_client: TestClient, initialized_db: str):
    """A signed user.created event → row in users table with email,
    display_name, profile_pic_url all populated from the payload."""
    payload = _user_created_payload()
    with patch("svix.webhooks.Webhook.verify", return_value=payload):
        r = webhook_client.post(
            "/webhooks/clerk",
            content=json.dumps(payload),
            headers={
                "svix-id": "msg_test",
                "svix-timestamp": "1",
                "svix-signature": "v1,fake",
            },
        )
    assert r.status_code == 200
    # Read the row directly — confirm shape.
    from prep.infrastructure.db import cursor

    with cursor() as c:
        row = c.execute("SELECT * FROM users WHERE tailscale_login = ?", ("user_2abc",)).fetchone()
    assert row is not None
    assert row["email"] == "alice@example.com"
    assert row["display_name"] == "Alice Anderson"
    assert row["profile_pic_url"] == "https://img/alice.png"


def test_webhook_user_updated_refreshes_row(webhook_client: TestClient, initialized_db: str):
    """Pre-seed a row, then send user.updated with a new email —
    the local row reflects the update."""
    # Seed with the initial email.
    payload1 = _user_created_payload(email="old@example.com")
    with patch("svix.webhooks.Webhook.verify", return_value=payload1):
        webhook_client.post(
            "/webhooks/clerk",
            content=json.dumps(payload1),
            headers={"svix-id": "1", "svix-timestamp": "1", "svix-signature": "v1,fake"},
        )
    # Now update.
    payload2 = _user_created_payload(email="new@example.com")
    payload2["type"] = "user.updated"
    with patch("svix.webhooks.Webhook.verify", return_value=payload2):
        webhook_client.post(
            "/webhooks/clerk",
            content=json.dumps(payload2),
            headers={"svix-id": "2", "svix-timestamp": "2", "svix-signature": "v1,fake"},
        )
    from prep.infrastructure.db import cursor

    with cursor() as c:
        row = c.execute(
            "SELECT email FROM users WHERE tailscale_login = ?", ("user_2abc",)
        ).fetchone()
    assert row["email"] == "new@example.com"


def test_webhook_user_deleted_hard_deletes_row(webhook_client: TestClient, initialized_db: str):
    """user.deleted → DELETE FROM users; FK cascade handles owned
    rows (we don't assert the cascade here — it's a foreign_keys=ON
    behavior already covered by other tests)."""
    # Seed first.
    created = _user_created_payload()
    with patch("svix.webhooks.Webhook.verify", return_value=created):
        webhook_client.post(
            "/webhooks/clerk",
            content=json.dumps(created),
            headers={"svix-id": "1", "svix-timestamp": "1", "svix-signature": "v1,fake"},
        )
    deleted = {"type": "user.deleted", "data": {"id": "user_2abc"}}
    with patch("svix.webhooks.Webhook.verify", return_value=deleted):
        r = webhook_client.post(
            "/webhooks/clerk",
            content=json.dumps(deleted),
            headers={"svix-id": "2", "svix-timestamp": "2", "svix-signature": "v1,fake"},
        )
    assert r.status_code == 200
    from prep.infrastructure.db import cursor

    with cursor() as c:
        row = c.execute("SELECT 1 FROM users WHERE tailscale_login = ?", ("user_2abc",)).fetchone()
    assert row is None


def test_webhook_bad_signature_returns_400(webhook_client: TestClient, initialized_db: str):
    """A signature that doesn't verify → 400. We don't process the
    payload, and Clerk's Svix transport stops retrying (it only
    retries on 5xx)."""
    from svix.webhooks import WebhookVerificationError

    with patch(
        "svix.webhooks.Webhook.verify",
        side_effect=WebhookVerificationError("bad sig"),
    ):
        r = webhook_client.post(
            "/webhooks/clerk",
            content="{}",
            headers={"svix-id": "x", "svix-timestamp": "x", "svix-signature": "x"},
        )
    assert r.status_code == 400


def test_webhook_unknown_event_type_is_ignored(webhook_client: TestClient, initialized_db: str):
    """Clerk sends session.created and other events too; we 200
    them so Svix stops retrying but don't touch the DB."""
    payload = {"type": "session.created", "data": {"id": "sess_xyz"}}
    with patch("svix.webhooks.Webhook.verify", return_value=payload):
        r = webhook_client.post(
            "/webhooks/clerk",
            content=json.dumps(payload),
            headers={"svix-id": "1", "svix-timestamp": "1", "svix-signature": "v1,fake"},
        )
    assert r.status_code == 200


def test_webhook_404s_when_secret_unset(client: TestClient, initialized_db: str):
    """The route isn't registered when CLERK_WEBHOOK_SECRET isn't
    set at app boot — pin that gate."""
    r = client.post("/webhooks/clerk", content="{}")
    assert r.status_code == 404


def test_user_created_with_no_email_still_upserts(webhook_client: TestClient, initialized_db: str):
    """A user without email_addresses (rare but valid — e.g. social
    sign-in where the provider didn't surface an email) — handler
    still records the user row with email=None instead of dropping."""
    payload = {
        "type": "user.created",
        "data": {"id": "user_noemail", "first_name": "Bob"},
    }
    with patch("svix.webhooks.Webhook.verify", return_value=payload):
        r = webhook_client.post(
            "/webhooks/clerk",
            content=json.dumps(payload),
            headers={"svix-id": "1", "svix-timestamp": "1", "svix-signature": "v1,fake"},
        )
    assert r.status_code == 200
    from prep.infrastructure.db import cursor

    with cursor() as c:
        row = c.execute(
            "SELECT email, display_name FROM users WHERE tailscale_login = ?",
            ("user_noemail",),
        ).fetchone()
    assert row is not None
    assert row["email"] is None
    assert row["display_name"] == "Bob"
