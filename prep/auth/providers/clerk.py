"""Clerk identity provider.

Used by the public prepcards.app deploy (PREP_AUTH_MODE=clerk). On
the mac-mini install this module is never imported — TailscaleProvider
is the default, and `prep.auth.providers.get_provider()` lazy-imports.

How it works:
- User signs in at https://accounts.prepcards.app (Clerk-hosted UI).
  Clerk sets a `__session` cookie on the parent domain.
- Every subsequent request to prepcards.app arrives with that cookie.
  We call `clerk_sdk.authenticate_request()` which verifies the JWT
  in the cookie (or Authorization header) without a round-trip to
  Clerk — keys come from JWKS, cached.
- The JWT's `sub` claim is the Clerk user_id; that becomes our
  `external_id`. Email + display_name come from the JWT claims when
  Clerk's default session template includes them; otherwise they're
  None on this request and the local `users` row already populated
  by the `user.created` webhook supplies them.

Required env:
- CLERK_SECRET_KEY            — backend API key (`sk_live_...` /
                                 `sk_test_...`); used to verify JWTs
- CLERK_AUTHORIZED_PARTIES    — comma-separated list of origins
                                 allowed to send requests (e.g.
                                 `https://prepcards.app`)
- CLERK_FRONTEND_API_URL      — base URL of the Clerk-hosted UI
                                 (e.g. `https://accounts.prepcards.app`),
                                 used to build sign-in / sign-out URLs

Optional env:
- CLERK_PUBLISHABLE_KEY       — public, currently unused server-side
                                 but stashed so future JS components
                                 can read it from a meta tag
"""

from __future__ import annotations

import logging
import os
from urllib.parse import quote_plus

from fastapi import Request

from prep.auth.port import AuthConfigError, IdentityProvider, ResolvedUser, SignInUrls

logger = logging.getLogger(__name__)


def _require_env(name: str) -> str:
    val = (os.environ.get(name) or "").strip()
    if not val:
        raise AuthConfigError(f"{name} must be set when PREP_AUTH_MODE=clerk")
    return val


class _RequestAdapter:
    """Minimal Requestish shim — Clerk's authenticate_request reads
    `.headers` (a Mapping with `Authorization` and `cookie`).
    FastAPI's `Headers` already satisfies that, but going through a
    plain dict avoids any Protocol-conformance surprises across SDK
    versions."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


class ClerkProvider(IdentityProvider):
    """Resolves identity from a Clerk session cookie / bearer token."""

    name = "clerk"

    def __init__(self) -> None:
        # Late import: the SDK pulls in httpx + jwt dependencies at
        # import time, and the mac-mini install (Tailscale mode) never
        # needs them. Keep them out of the cold path.
        from clerk_backend_api import AuthenticateRequestOptions, Clerk

        self._sdk = Clerk(bearer_auth=_require_env("CLERK_SECRET_KEY"))
        parties = _require_env("CLERK_AUTHORIZED_PARTIES")
        self._authorized_parties = [p.strip() for p in parties.split(",") if p.strip()]
        # Trailing slash off — we append explicit paths.
        self._frontend = _require_env("CLERK_FRONTEND_API_URL").rstrip("/")
        # Stash for callers (e.g. JS-emitting templates can read it
        # from request.app.state). Not strictly required.
        self._publishable = (os.environ.get("CLERK_PUBLISHABLE_KEY") or "").strip()
        self._options_cls = AuthenticateRequestOptions

    def resolve(self, request: Request) -> ResolvedUser | None:
        # FastAPI's request.headers is case-insensitive but Clerk's
        # auth code uses both lowercase ('cookie') and mixed-case
        # ('Authorization') — copy into a plain dict that preserves
        # both spellings so the lookup never misses.
        headers = {}
        for k, v in request.headers.items():
            headers[k] = v
            headers[k.lower()] = v
        adapter = _RequestAdapter(headers)
        try:
            state = self._sdk.authenticate_request(
                adapter,
                self._options_cls(authorized_parties=self._authorized_parties),
            )
        except Exception:  # noqa: BLE001 — funnel any SDK fault as "unauthenticated"
            logger.warning("clerk authenticate_request failed", exc_info=True)
            return None
        if not state.is_signed_in:
            return None
        payload = state.payload or {}
        external_id = payload.get("sub")
        if not external_id:
            logger.warning("clerk request signed-in but JWT has no sub claim")
            return None
        # Email / name may or may not be in the JWT depending on the
        # session template configured in the Clerk dashboard. When
        # absent, the local users row (populated by the user.created
        # webhook) supplies them — see prep/auth/webhooks_clerk.py.
        return ResolvedUser(
            external_id=external_id,
            email=payload.get("email") or payload.get("primary_email") or None,
            display_name=(
                payload.get("name") or payload.get("full_name") or payload.get("username") or None
            ),
            profile_pic_url=payload.get("picture") or payload.get("image_url") or None,
            provider=self.name,
        )

    def has_dormant_session(self, request: Request) -> bool:
        # __client_uat is Clerk's durable "user auth timestamp" cookie,
        # set on the app's eTLD+1 precisely so servers can see client
        # session state without the (FAPI-domain) __client cookie. A
        # non-zero value means ClerkJS holds a live client session even
        # though the ~60s __session JWT may have expired -- Clerk's
        # "handshake" state. "0" / absent means genuinely signed out.
        uat = request.cookies.get("__client_uat")
        return bool(uat and uat.strip() != "0")

    def urls(self) -> SignInUrls:
        # Clerk's hosted UI lives at the configured frontend URL.
        # /sign-in + /sign-out + /user are the conventional paths;
        # we pass redirect_url so the user lands back on prep after.
        # Templates substitute the current page URL into ?return_to
        # before navigating.
        return SignInUrls(
            sign_in=f"{self._frontend}/sign-in?redirect_url={quote_plus('https://prepcards.app/')}",
            sign_out=f"{self._frontend}/sign-out?redirect_url={quote_plus('https://prepcards.app/')}",
            account=f"{self._frontend}/user",
        )

    # ---- introspection helpers (used by routes + webhooks) -------------

    @property
    def secret_key(self) -> str:
        """Exposed so the webhook handler can fall back to a Clerk
        API call if the webhook arrives before the user row exists."""
        # Cached via _sdk's auth header; pulling from env directly is
        # cheap and avoids exposing the SDK internals.
        return _require_env("CLERK_SECRET_KEY")

    @property
    def publishable_key(self) -> str:
        return self._publishable
