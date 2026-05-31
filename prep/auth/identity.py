"""Auth dependency module.

Per-request user resolution is delegated to whichever
`IdentityProvider` is active for this deploy — Tailscale headers on
the mac mini, Clerk on the public VPS, a fake in tests. The provider
is chosen at boot via `PREP_AUTH_MODE` (default `tailscale`). See
`prep/auth/port.py` for the abstraction and `providers/*.py` for
the adapters.

This module stays a flat import target so any router can
`from prep.auth import current_user` without going through app.py
(which would cycle back through the router on import).
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from prep.auth.providers import get_provider
from prep.auth.repo import UserRepo


def current_user(request: Request) -> dict:
    """FastAPI dependency: resolve the request's user, or 401.

    Provider-agnostic — TailscaleProvider returns a ResolvedUser
    from Tailscale headers; ClerkProvider returns one from a Clerk
    session cookie; FakeProvider returns a pinned test user. The
    rest of this function doesn't branch on which provider it is.

    Side effect: upserts the user into the `users` table (display
    name + last_seen_at + email stay fresh) and stashes the resolved
    DB row on `request.state.user` for the Jinja context_processor
    in app.py to surface to every template.
    """
    user = optional_current_user(request)
    if user is None:
        raise HTTPException(401, "not authenticated")
    return user


def optional_current_user(request: Request) -> dict | None:
    """Variant of `current_user` that returns None for unauthenticated
    requests instead of raising 401. Used by routes that have a
    public branch (the landing page) AND a signed-in branch (the
    dashboard) — same URL, different render."""
    resolved = get_provider().resolve(request)
    if not resolved:
        return None
    user = UserRepo().upsert(
        external_id=resolved.external_id,
        email=resolved.email,
        display_name=resolved.display_name,
        profile_pic_url=resolved.profile_pic_url,
    )
    request.state.user = user
    return user
