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


class SignInRequired(Exception):
    """An anonymous account reached a surface that needs a durable
    identity. Translated by the handler in `prep.web.errors`: a
    redirect to sign-in for HTML, 403 for JSON."""


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


def signed_in_user(request: Request) -> dict:
    """current_user, plus: anonymous accounts are refused. For
    surfaces that need a durable, provable identity (a push endpoint
    that outlives the cookie, a secret we must protect, a token that
    authenticates elsewhere) and for surfaces whose cost is unbounded
    per account.

    Applied per route, never to a whole router: a route nobody
    thought about must not inherit a gate by accident."""
    user = current_user(request)
    if user.get("is_anonymous"):
        raise SignInRequired()
    return user


def optional_current_user(request: Request) -> dict | None:
    """Variant of `current_user` that returns None for unauthenticated
    requests instead of raising 401. Used by routes that have a
    public branch (the landing page) AND a signed-in branch (the
    dashboard) — same URL, different render."""
    resolved = get_provider().resolve(request)
    if not resolved:
        return None
    repo = UserRepo()
    if resolved.is_anonymous:
        # Never upsert an anonymous id: upsert inserts on miss, so a
        # cookie naming a reaped account would resurrect it as an
        # empty user forever. Anonymous rows are created at mint time
        # only; a missing row means the cookie is dead. So is a row
        # that no longer carries the flag: every downstream gate reads
        # the ROW, so honouring the cookie alone would turn a cleared
        # flag into an unrestricted session for whoever still holds it.
        user = repo.get_by_external_id(resolved.external_id)
        if user is None or not user.get("is_anonymous"):
            request.state.anon_cookie_stale = True
            return None
        repo.touch(resolved.external_id)
    else:
        user = repo.upsert(
            external_id=resolved.external_id,
            email=resolved.email,
            display_name=resolved.display_name,
            profile_pic_url=resolved.profile_pic_url,
        )
    request.state.user = user
    return user
