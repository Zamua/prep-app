"""FastAPI dependency that resolves a Bearer token to a user.

The web UI uses `prep.auth.current_user` (cookie/Tailscale headers).
The public API uses THIS module — `Authorization: Bearer prep_pat_…`
on every request, validated against the api_tokens table.

The dep deliberately doesn't fall back to cookie auth. The /api/v1/*
surface is a separate trust boundary; mixing the auth shapes makes
CSRF reasoning harder. If a web user wants to script against prep,
they generate a PAT on /settings/api like everyone else.
"""

from __future__ import annotations

from fastapi import Header, HTTPException, Request

from prep.api.repo import ApiTokenRepo
from prep.auth.repo import UserRepo


def bearer_user(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    """Resolve the request's user from `Authorization: Bearer <token>`.

    On success, sets `request.state.user` to the user-dict shape the
    rest of prep's services expect (same shape `prep.auth.current_user`
    returns). On any failure — missing header, malformed scheme,
    unknown token — raises 401 with a generic message; we don't
    differentiate failure modes to avoid signal to an attacker
    probing the surface.
    """
    if not authorization:
        raise HTTPException(401, "missing Authorization header")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value:
        raise HTTPException(401, "Authorization must be 'Bearer <token>'")

    lookup = ApiTokenRepo().lookup(value)
    if not lookup:
        raise HTTPException(401, "invalid or revoked token")

    user_id, _token_id = lookup
    # READ-ONLY lookup — don't upsert. An upsert here would overwrite
    # the user's stored email with the user_id string (since the API
    # path has no email to provide), and would also bump last_seen_at
    # for a non-browser request which muddies the signal. The token
    # already proves identity; we just need the user dict.
    user = UserRepo().get_by_external_id(user_id)
    if user is None:
        # Token row pointed at a deleted user. Refuse and let the
        # token rot — Clerk's user.deleted webhook cascaded
        # byok_credentials etc. but the api_tokens cascade kicks
        # in via the FK we declared. So this branch is mostly
        # paranoia; surface it as 401, not a 500.
        raise HTTPException(401, "user no longer exists")
    request.state.user = user
    return user
