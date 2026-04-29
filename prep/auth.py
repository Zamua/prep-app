"""Auth dependency module.

Tailscale Serve injects per-user identity headers when fronting the
deploy. We read them off the Request, fall back to PREP_DEFAULT_USER
for unfronted local-dev and single-user setups, and 401 otherwise.

Right now this is a flat module so any router can import
`current_user` without going through app.py (which would cycle
back through the router on import). Phase 9 turns this into a
proper auth bounded-context package (auth/identity.py +
auth/ownership.py) once we have the second user-protected resource
that needs declarative ownership checks.
"""

from __future__ import annotations

import os

from fastapi import HTTPException, Request

from prep import db


def _resolve_login(request: Request) -> str | None:
    """The user's identity, or None if we can't determine one.

    Tailscale headers always win. PREP_DEFAULT_USER is the dev-time
    bypass — empty/unset means "no bypass; require real auth".
    """
    hdr = request.headers.get("tailscale-user-login")
    if hdr:
        return hdr.strip()
    fallback = os.environ.get("PREP_DEFAULT_USER")
    return fallback or None


def current_user(request: Request) -> dict:
    """FastAPI dependency: resolve the request's user, or 401.

    Side effect: upserts the user into the `users` table (so display
    name + last_seen_at are kept fresh) and stashes the resolved user
    dict on `request.state.user` for the Jinja context_processor in
    app.py to surface to every template.
    """
    login = _resolve_login(request)
    if not login:
        raise HTTPException(
            401, "no Tailscale identity (set Tailscale-User-Login header or PREP_DEFAULT_USER)"
        )
    display_name = request.headers.get("tailscale-user-name") or login.split("@", 1)[0]
    profile_pic = request.headers.get("tailscale-user-profile-pic") or None
    user = db.upsert_user(login, display_name, profile_pic)
    request.state.user = user
    return user
