"""Clerk webhook receiver.

Clerk pushes `user.*` events via Svix-signed HTTP POST to keep our
local `users` table mirrored with their identity store. The
canonical write path: a new user signs up via Clerk's hosted UI →
Clerk fires `user.created` → we land a `users` row with their email
+ display_name BEFORE their first prep request arrives. Subsequent
`user.updated` events keep the mirror fresh (email rotation, name
change). `user.deleted` cascades through the FK chain and wipes
their decks + sessions + reviews.

This module is only mounted when CLERK_WEBHOOK_SECRET is set (see
prep/app.py). On Tailscale-mode deploys the env var is absent, the
endpoint isn't registered, and the clerk-backend-api / svix imports
never run.

Required env:
- CLERK_WEBHOOK_SECRET — Svix signing secret from the Clerk
  dashboard (Webhooks → your endpoint → Signing Secret)
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, HTTPException, Request

from prep.auth.repo import UserRepo
from prep.infrastructure.db import cursor

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/webhooks/clerk")
async def clerk_webhook(request: Request):
    """Receive + verify a Clerk webhook event, then mirror the
    user change locally.

    Returns 204 on success, 400 on bad signature, 422 on malformed
    payload. We never return 5xx for known-bad input — Clerk's
    Svix transport retries 5xx aggressively, and a malformed payload
    isn't going to fix itself."""
    secret = (os.environ.get("CLERK_WEBHOOK_SECRET") or "").strip()
    if not secret:
        # Defensive — the route shouldn't be mounted without the env
        # var, but if someone wires it up by hand, return 503 rather
        # than dropping events silently.
        raise HTTPException(503, "CLERK_WEBHOOK_SECRET not configured")
    # Late import: svix is only needed when this route is mounted.
    from svix.webhooks import Webhook, WebhookVerificationError

    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        payload = Webhook(secret).verify(body, headers)
    except WebhookVerificationError as e:
        logger.warning("clerk webhook signature verify failed: %s", e)
        raise HTTPException(400, "invalid signature") from e

    event_type = payload.get("type")
    data = payload.get("data") or {}
    if not event_type or not data:
        raise HTTPException(422, "malformed payload (missing type/data)")

    if event_type in ("user.created", "user.updated"):
        _mirror_user(data)
    elif event_type == "user.deleted":
        _delete_user(data)
    else:
        # Other event types we don't act on (session.created etc.)
        # but should still 204 so Clerk stops retrying.
        logger.debug("clerk webhook: ignoring event type %s", event_type)

    return ""  # FastAPI returns 200 with an empty body; close enough


def _primary_email(user_data: dict) -> str | None:
    """Pull the user's primary email address from a Clerk user object.
    Clerk sends `email_addresses: [{id, email_address, …}]` plus a
    `primary_email_address_id` pointer."""
    pid = user_data.get("primary_email_address_id")
    for addr in user_data.get("email_addresses") or []:
        if addr.get("id") == pid:
            return addr.get("email_address")
    # Fallback: first email in the list, if any.
    emails = user_data.get("email_addresses") or []
    if emails:
        return emails[0].get("email_address")
    return None


def _display_name(user_data: dict) -> str | None:
    """Best-effort name for the UI chip. Clerk gives us first_name +
    last_name; we also fall back to username, then the local part of
    the email."""
    first = (user_data.get("first_name") or "").strip()
    last = (user_data.get("last_name") or "").strip()
    full = (first + " " + last).strip()
    if full:
        return full
    if user_data.get("username"):
        return user_data["username"]
    email = _primary_email(user_data)
    if email:
        return email.split("@", 1)[0]
    return None


def _mirror_user(user_data: dict) -> None:
    """Upsert the local users row from a Clerk user.{created,updated}
    payload. `id` is the Clerk user_id; we use it as the external_id
    so the same value flows through `request.state.user`."""
    user_id = user_data.get("id")
    if not user_id:
        raise HTTPException(422, "user payload missing id")
    UserRepo().upsert(
        external_id=user_id,
        email=_primary_email(user_data),
        display_name=_display_name(user_data),
        profile_pic_url=user_data.get("image_url") or user_data.get("profile_image_url"),
    )


def _delete_user(user_data: dict) -> None:
    """Hard-delete the user. The FK chain cascades through decks →
    questions → cards → reviews → study_sessions / trivia_sessions /
    notifications_log / push_subscriptions. Same shape Tailscale
    auth would use for a hypothetical /delete-account button."""
    user_id = user_data.get("id")
    if not user_id:
        raise HTTPException(422, "delete payload missing id")
    with cursor() as c:
        c.execute("DELETE FROM users WHERE tailscale_login = ?", (user_id,))
