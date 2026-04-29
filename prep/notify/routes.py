"""HTTP routes for the notify bounded context.

The browser hits these to:
- Render the settings page (GET /notify)
- Persist user preferences (POST /notify/prefs)
- Fetch the VAPID public key for subscribing (GET /notify/vapid-public-key)
- Register / unregister a device (POST /notify/{subscribe,unsubscribe})
- Send a one-off "test push" (POST /notify/test)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError

from prep import notify as _notify_pkg
from prep.auth import current_user
from prep.notify.entities import NotificationPrefs
from prep.notify.repo import NotifyPrefsRepo, PushSubsRepo
from prep.web.templates import templates

router = APIRouter()


def _prefs_repo() -> NotifyPrefsRepo:
    return NotifyPrefsRepo()


def _subs_repo() -> PushSubsRepo:
    return PushSubsRepo()


@router.get("/notify", response_class=HTMLResponse)
def notify_settings(
    request: Request,
    user: dict = Depends(current_user),
    prefs_repo: NotifyPrefsRepo = Depends(_prefs_repo),
    subs_repo: PushSubsRepo = Depends(_subs_repo),
):
    uid = user["tailscale_login"]
    prefs = prefs_repo.get(uid)
    devices = subs_repo.count_for_user(uid)
    return templates.TemplateResponse(
        "notify_settings.html",
        {
            "request": request,
            "user": user,
            "prefs": prefs.model_dump(),
            "devices": devices,
            "vapid_key": _notify_pkg.public_key_b64(),
        },
    )


@router.post("/notify/prefs")
async def notify_prefs_save(
    request: Request,
    user: dict = Depends(current_user),
    prefs_repo: NotifyPrefsRepo = Depends(_prefs_repo),
):
    """Merge submitted values over the existing prefs so scheduler-only
    fields (last_digest_date, last_when_ready_at) survive an update.
    pydantic does the bounds-check + mode validation."""
    uid = user["tailscale_login"]
    raw = await request.json()
    if not isinstance(raw, dict):
        raise HTTPException(400, "expected an object")

    existing = prefs_repo.get(uid)
    merged = {**existing.model_dump(), **raw}
    # Preserve scheduler-managed state untouched even if the client
    # sent dummy values for them.
    merged["last_digest_date"] = existing.last_digest_date
    merged["last_when_ready_at"] = existing.last_when_ready_at

    try:
        prefs = NotificationPrefs.model_validate(merged)
    except ValidationError as e:
        raise HTTPException(422, e.errors())

    prefs_repo.set(uid, prefs)
    return JSONResponse({"ok": True, "prefs": prefs.model_dump()})


@router.get("/notify/vapid-public-key")
def vapid_public_key():
    return JSONResponse({"key": _notify_pkg.public_key_b64()})


@router.post("/notify/subscribe")
async def notify_subscribe(request: Request, user: dict = Depends(current_user)):
    """Browser-supplied subscription payload: {endpoint, keys: {p256dh, auth}}.
    Stored via the public surface in prep.notify so future repo extraction
    of the underlying SQL is invisible to the route layer."""
    sub = await request.json()
    if not isinstance(sub, dict) or "endpoint" not in sub:
        raise HTTPException(400, "bad subscription payload")
    _notify_pkg.subscribe(user["tailscale_login"], sub)
    return JSONResponse({"ok": True})


@router.post("/notify/unsubscribe")
async def notify_unsubscribe(
    request: Request,
    user: dict = Depends(current_user),
    subs_repo: PushSubsRepo = Depends(_subs_repo),
):
    """Remove a single device's push subscription. Endpoint is the
    natural key — same endpoint can only belong to one user."""
    body = await request.json()
    endpoint = body.get("endpoint") if isinstance(body, dict) else None
    if not endpoint:
        raise HTTPException(400, "missing endpoint")
    subs_repo.delete_by_endpoint(endpoint)
    return JSONResponse({"ok": True})


@router.post("/notify/test")
async def notify_send_test(user: dict = Depends(current_user)):
    """Send a one-off test push to the current user's devices so they
    can verify subscription is alive end-to-end."""
    res = _notify_pkg.send_to_user(
        user["tailscale_login"],
        "Prep — test push",
        "If you can read this, notifications are working on this device.",
        url="/notify",
    )
    return JSONResponse(res)
