"""Web Push (VAPID) — key management + per-user fanout.

This module handles the I/O side of the notify context: VAPID keypair
bootstrap, single-subscription send, multi-device fanout for one user,
subscription persistence, and stale-subscription pruning. It does NOT
own scheduling — that's `prep.notify.scheduler`.

Public API consumed by app.py + routes:
- `public_key_b64()`           VAPID app server key for the browser
- `subscribe(user_id, sub)`    persist or refresh a browser subscription
- `send_to_user(user_id, ...)` fanout push to all of one user's devices

VAPID keys live on disk (`vapid-private.pem` + `vapid-keys.json`),
gitignored, generated on first boot, reused after. Paths are
overridable via `PREP_VAPID_KEYS_PATH` / `PREP_VAPID_PEM_PATH` so the
artifact-based deploy can park them in a persistent data dir outside
the immutable image.
"""

from __future__ import annotations

import json
import logging
import os as _os
import threading
from pathlib import Path

from py_vapid import Vapid01
from pywebpush import WebPushException, webpush

from prep.notify.repo import PushSubsRepo

# The notify package lives at <repo>/prep/notify/, but VAPID keys
# default to the repo root (alongside data.sqlite) for the dev case.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_KEYS_PATH = Path(_os.environ.get("PREP_VAPID_KEYS_PATH") or (_REPO_ROOT / "vapid-keys.json"))
_KEY_PEM_PATH = Path(_os.environ.get("PREP_VAPID_PEM_PATH") or (_REPO_ROOT / "vapid-private.pem"))

# IANA "sub" claim for VAPID. Push services use this as a contact for
# operational issues. Public deployments should set PREP_VAPID_SUB to a
# real address; we fall back to the placeholder for local/dev.
VAPID_SUB = _os.environ.get("PREP_VAPID_SUB", "mailto:noreply@example.com")

_log = logging.getLogger("prep.notify")
_lock = threading.Lock()


# ---- VAPID key bootstrap --------------------------------------------------


def _public_key_b64url(vapid: Vapid01) -> str:
    import base64

    from cryptography.hazmat.primitives import serialization

    raw = vapid.public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _load_or_create_keys() -> dict:
    """Returns {vapid: Vapid01, public_b64: str}. pywebpush's
    `vapid_private_key` arg wants either a Vapid01 instance or a *file
    path* — passing a PEM as a string fails because py_vapid's
    from_string expects raw base64url (not PEM). So we store the PEM as
    its own file and load it via Vapid.from_file each time."""
    if not _KEY_PEM_PATH.exists():
        v = Vapid01()
        v.generate_keys()
        _KEY_PEM_PATH.write_bytes(v.private_pem())
        _KEYS_PATH.write_text(json.dumps({"public_b64": _public_key_b64url(v)}, indent=2))
    meta = json.loads(_KEYS_PATH.read_text()) if _KEYS_PATH.exists() else {}
    vapid = Vapid01.from_file(str(_KEY_PEM_PATH))
    if "public_b64" not in meta:
        meta["public_b64"] = _public_key_b64url(vapid)
        _KEYS_PATH.write_text(json.dumps(meta, indent=2))
    return {"vapid": vapid, "public_b64": meta["public_b64"]}


def public_key_b64() -> str:
    return _load_or_create_keys()["public_b64"]


# ---- Send glue ------------------------------------------------------------


def _send_one(sub_row: dict, payload: dict) -> str:
    """Send a single push. Returns 'ok', 'gone' (subscription invalid;
    caller should prune), or 'fail'. Never raises."""
    keys = _load_or_create_keys()
    sub = {
        "endpoint": sub_row["endpoint"],
        "keys": {"p256dh": sub_row["p256dh"], "auth": sub_row["auth"]},
    }
    try:
        webpush(
            subscription_info=sub,
            data=json.dumps(payload),
            vapid_private_key=keys["vapid"],
            vapid_claims={"sub": VAPID_SUB},
            ttl=60,
        )
        return "ok"
    except WebPushException as e:
        status = getattr(e.response, "status_code", None) if e.response is not None else None
        if status in (404, 410):
            return "gone"
        _log.warning(
            "webpush failed: status=%s body=%s",
            status,
            e.response.text[:200] if e.response is not None else "",
        )
        return "fail"
    except Exception as e:
        _log.warning("webpush error: %s", e)
        return "fail"


def send_to_user(
    user_id: str,
    title: str,
    body: str,
    url: str | None = None,
    *,
    source: str = "manual",
    tag: str | None = None,
) -> dict:
    """Send a push to every device the user has subscribed. Prunes any
    subscriptions the push service rejects with 404/410. Returns counts.
    Tap-target URL defaults to the app root.

    Callers pass an *app-relative* URL (`/trivia/session/foo`, `/`).
    The deploy's ROOT_PATH (e.g. `/prep` or `/prep-staging`) gets
    prepended here so the SW's notificationclick handler — which
    treats the URL as origin-absolute — actually lands inside the
    PWA scope. Without the prefix, iOS sends the user to the
    site root and (since that's outside scope) the PWA bounces
    them to its start_url instead of the intended page.

    `source` ("trivia" | "srs-digest" | "srs-when-ready" | "manual")
    is recorded in the notification log so the user can filter; defaults
    to "manual" for ad-hoc test pushes.

    `tag` populates the SW's `showNotification({tag})` slot. iOS uses
    it to coalesce stacked notifications: a new push with an existing
    tag replaces the prior one instead of stacking under the app icon.
    Pass per-deck (`trivia-<name>`) for trivia, leave `None` for
    one-offs (the SW falls back to "prep-default").
    """
    root = (_os.environ.get("ROOT_PATH") or "").rstrip("/")
    raw = url or "/"
    if raw.startswith("/") and not raw.startswith(root + "/") and raw != root:
        raw = root + raw if root else raw
    payload: dict = {"title": title, "body": body, "url": raw}
    if tag:
        payload["tag"] = tag
    # Append to the log BEFORE delivery so a subsequent failure still
    # leaves the user with a record of what was attempted.
    try:
        from prep.notify.repo import NotificationLogRepo

        NotificationLogRepo().append(
            user_id=user_id, title=title, body=body, url=raw, source=source
        )
    except Exception as e:
        _log.warning("notification log append failed: %s", e)
    repo = PushSubsRepo()
    subs = repo.list_for_user_raw(user_id)
    sent = failed = pruned = 0
    for s in subs:
        result = _send_one(s, payload)
        if result == "ok":
            sent += 1
        elif result == "gone":
            repo.delete_by_endpoint(s["endpoint"])
            pruned += 1
        else:
            failed += 1
    return {"sent": sent, "failed": failed, "pruned": pruned}


# ---- Subscribe ------------------------------------------------------------


def subscribe(user_id: str, sub: dict) -> None:
    """Store the browser's push subscription. Called by /notify/subscribe."""
    endpoint = sub.get("endpoint")
    keys = sub.get("keys", {})
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not (endpoint and p256dh and auth):
        raise ValueError("subscription missing endpoint/keys.p256dh/keys.auth")
    PushSubsRepo().upsert(user_id, endpoint, p256dh, auth)
