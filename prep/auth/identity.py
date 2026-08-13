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

import logging

from fastapi import HTTPException, Request

from prep.auth.anon_cookie import COOKIE_NAME as ANON_COOKIE
from prep.auth.anon_cookie import verify_cookie
from prep.auth.merge import merge_anonymous_into
from prep.auth.providers import get_provider
from prep.auth.repo import UserRepo

logger = logging.getLogger(__name__)


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
        # After the upsert, never before: the merge takes the id of
        # the row upsert just wrote, so running it first makes every
        # fresh sign-up a no-op that silently loses the account.
        if _try_merge_anon_cookie(request, user):
            # The merge may have carried preferences onto this row.
            user = repo.get_by_external_id(user["tailscale_login"]) or user
    request.state.user = user
    return user


def _try_merge_anon_cookie(request: Request, user: dict) -> bool:
    """Merge the browser's anonymous account into the signed-in one,
    and never fail the request while doing it. Returns True when data
    moved.

    Every route reaches this through `current_user`, so an uncaught
    exception here turns every authenticated request carrying an anon
    cookie into a 500. Integrity is enforced by the transaction
    rolling back, availability by the blanket except: a lock timeout
    or a tripped guard keeps the cookie and retries next request."""
    raw = request.cookies.get(ANON_COOKIE)
    if not raw:
        return False
    cookie = verify_cookie(raw)
    if cookie is None:
        return False
    target_id = user["tailscale_login"]
    if cookie.external_id == target_id:
        # The cookie names the account it is presented on, so it points
        # at a row that is no longer anonymous. Nothing to move, and
        # nothing left for the cookie to be.
        request.state.anon_cookie_stale = True
        return False
    try:
        result = merge_anonymous_into(cookie.external_id, target_id)
    except Exception:
        logger.exception("anon merge failed: anon=%s", cookie.external_id)
        return False
    if not result.resolved:
        # The cookie still names a live account holding real decks.
        logger.warning(
            "anon merge unresolved: anon=%s reason=%s", cookie.external_id, result.reason
        )
        return False
    request.state.anon_cookie_stale = True
    if result.merged:
        request.state.anon_merged = result.counts
    return result.merged
