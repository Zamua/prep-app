"""Web Push (VAPID) integration for the prep-app.

Subscriptions live in the `push_subscriptions` SQLite table (one row per
device, keyed by endpoint, FK to users.tailscale_login). VAPID keypair
lives on disk (`vapid-private.pem` + `vapid-keys.json`); both are
gitignored. Generated on first boot, reused after.

Public surface used by app.py:
- `public_key_b64()`           — VAPID app server key for the browser
- `subscribe(user_id, sub)`    — store/refresh a subscription
- `send_to_user(user_id, ...)` — push to all of one user's devices
- `start_scheduler(loop)`      — launch the periodic digest / when-ready check

The scheduler is intentionally simple: wake every 5 minutes, walk the set
of users who have subscriptions, decide if the current minute is the
right moment to fire (per their prefs), send if so, record `last_*`
state in the user's notification_prefs JSON to dedupe.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os as _os
import threading
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from py_vapid import Vapid01
from pywebpush import WebPushException, webpush

from prep import db

# Key paths can be overridden via env so the artifact-based deploy can
# keep VAPID keys in a persistent data dir outside the immutable artifact
# (e.g. ~/Library/prep/data/<env>/vapid-*). Public-key + subscriptions
# are tied to the keypair, so persisting them across deploys avoids
# invalidating every push subscription on each promote.
# The package lives at <repo>/prep/, but VAPID keys default to the
# repo root (alongside data.sqlite) for the dev case.
_REPO_ROOT = Path(__file__).resolve().parent.parent
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


def send_to_user(user_id: str, title: str, body: str, url: str | None = None) -> dict:
    """Send a push to every device the user has subscribed. Prunes any
    subscriptions the push service rejects with 404/410. Returns counts.
    Tap-target URL defaults to the app root."""
    payload = {"title": title, "body": body, "url": url or "/"}
    subs = db.list_push_subscriptions(user_id)
    sent = failed = pruned = 0
    for s in subs:
        result = _send_one(s, payload)
        if result == "ok":
            sent += 1
        elif result == "gone":
            db.delete_push_subscription(s["endpoint"])
            pruned += 1
        else:
            failed += 1
    return {"sent": sent, "failed": failed, "pruned": pruned}


# ---- Subscribe / migrate from old file storage ----------------------------


def subscribe(user_id: str, sub: dict) -> None:
    """Store the browser's push subscription. Called by /notify/subscribe."""
    endpoint = sub.get("endpoint")
    keys = sub.get("keys", {})
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not (endpoint and p256dh and auth):
        raise ValueError("subscription missing endpoint/keys.p256dh/keys.auth")
    db.upsert_push_subscription(user_id, endpoint, p256dh, auth)


# ---- Scheduler ------------------------------------------------------------
#
# Wakes every 5 minutes. For each user with subscriptions and mode != off,
# evaluates whether to send. Idempotency lives in the user's prefs JSON
# (last_digest_date, last_when_ready_at). Quiet hours respected for both
# modes — we won't fire pushes during the configured night window even if
# the digest hour falls inside it.

_TICK_SECONDS = 300  # 5 minutes
_WHEN_READY_DEBOUNCE_SECONDS = 4 * 60 * 60  # don't re-fire within 4h


def _in_quiet_hours(local_hour: int, quiet_start: int, quiet_end: int) -> bool:
    if quiet_start == quiet_end:
        return False
    if quiet_start < quiet_end:
        return quiet_start <= local_hour < quiet_end
    # Wraps midnight (e.g., 22..8): quiet from start..24 OR 0..end
    return local_hour >= quiet_start or local_hour < quiet_end


def _digest_body(deck_breakdown: list[tuple[str, int]], total: int) -> str:
    if total == 0:
        return ""
    if len(deck_breakdown) == 1:
        n, name = deck_breakdown[0][1], deck_breakdown[0][0]
        return f"{n} card{'s' if n != 1 else ''} due in {name}."
    head = ", ".join(f"{n} in {name}" for name, n in deck_breakdown[:3])
    extra = "" if len(deck_breakdown) <= 3 else f", + {len(deck_breakdown) - 3} more"
    return f"{total} cards due — {head}{extra}."


def _should_send_digest(prefs: dict, local_now: datetime) -> bool:
    """Digest: send once per local day at the configured hour. Allow a
    grace window so a 5-minute scheduler tick doesn't have to land exactly
    on the hour."""
    if prefs.get("mode") != "digest":
        return False
    target_hour = int(prefs.get("digest_hour", 9))
    # 5-min tick — if local hour matches, we're in the window. Don't
    # re-send if we already sent this local date.
    if local_now.hour != target_hour:
        return False
    today_iso = local_now.date().isoformat()
    return prefs.get("last_digest_date") != today_iso


def _should_send_when_ready(prefs: dict, due_total: int, now_utc: datetime) -> bool:
    if prefs.get("mode") != "when-ready":
        return False
    threshold = int(prefs.get("threshold", 3))
    if due_total < threshold:
        return False
    last = prefs.get("last_when_ready_at")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            if (now_utc - last_dt).total_seconds() < _WHEN_READY_DEBOUNCE_SECONDS:
                return False
        except ValueError:
            pass
    return True


async def _tick() -> None:
    """One scheduler iteration — evaluate every subscribed user and fire
    where appropriate."""
    now_utc = datetime.now(timezone.utc)
    for uid in db.list_users_with_push_subs():
        try:
            prefs = db.get_notification_prefs(uid)
            if prefs.get("mode") == "off":
                continue

            tz_name = prefs.get("tz", "America/New_York")
            try:
                tz = ZoneInfo(tz_name)
            except Exception:
                tz = ZoneInfo("America/New_York")
            local = now_utc.astimezone(tz)

            due_total = db.count_due_for_user(uid)
            if due_total == 0:
                continue

            mode = prefs.get("mode")
            if mode == "digest":
                # Digest fires at the user's deliberately-chosen hour — quiet
                # hours don't apply (the chosen hour IS the schedule). Idempotent
                # via last_digest_date so a 5-minute tick window won't double-fire.
                if _should_send_digest(prefs, local):
                    breakdown = db.deck_due_breakdown(uid)
                    send_to_user(
                        uid, "Prep — daily digest", _digest_body(breakdown, due_total), url="/"
                    )
                    prefs["last_digest_date"] = local.date().isoformat()
                    db.set_notification_prefs(uid, prefs)
            elif mode == "when-ready":
                # Quiet hours apply here — when-ready can fire any time and the
                # user might not want a 3am ping for a card that just rolled due.
                # Honor the opt-in: skip the check entirely if disabled.
                if prefs.get("quiet_hours_enabled") and _in_quiet_hours(
                    local.hour,
                    int(prefs.get("quiet_start_hour", 22)),
                    int(prefs.get("quiet_end_hour", 8)),
                ):
                    continue
                if _should_send_when_ready(prefs, due_total, now_utc):
                    send_to_user(
                        uid,
                        "Prep — cards ready",
                        f"{due_total} card{'s' if due_total != 1 else ''} due to study.",
                        url="/",
                    )
                    prefs["last_when_ready_at"] = now_utc.isoformat()
                    db.set_notification_prefs(uid, prefs)
        except Exception as e:
            _log.exception("scheduler tick failed for user %s: %s", uid, e)

    # ---- trivia decks --------------------------------------------------
    # Distinct from the per-user srs digest/when-ready logic above. Each
    # trivia deck has its own per-deck interval (`notification_interval_
    # minutes`), so the dispatch logic lives in `prep.trivia.scheduler`
    # rather than inlined here. Late import keeps notify → trivia
    # one-way (trivia.scheduler imports send_to_user from us).
    try:
        from prep.trivia.scheduler import tick as _trivia_tick

        _trivia_tick(now_utc)
    except Exception as e:
        _log.exception("trivia scheduler tick threw: %s", e)


async def _scheduler_loop() -> None:
    while True:
        try:
            await _tick()
        except Exception as e:
            _log.exception("scheduler tick threw: %s", e)
        await asyncio.sleep(_TICK_SECONDS)


def start_scheduler() -> None:
    """Launch the background scheduler task on the running event loop.
    Call once from app startup. Idempotent — a second call is a no-op."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
    if getattr(start_scheduler, "_started", False):
        return
    loop.create_task(_scheduler_loop())
    start_scheduler._started = True
    _log.info("notification scheduler started (tick=%ss)", _TICK_SECONDS)
