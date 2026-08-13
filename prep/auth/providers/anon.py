"""Anonymous-cookie fallback, wrapped around the configured provider.

The precedence rule lives here in full so no call site has to
remember it: signed-in > dormant session > anonymous cookie >
visitor.

The dormant-session step is load-bearing. A returning provider user
on a PWA cold launch has an expired session token and durable
evidence of one, so the inner `resolve()` returns None while
`has_dormant_session()` is True. Falling through to a `prep_anon`
cookie left on that browser would serve a signed-in person their old
anonymous account and stop every recovery path keyed on "no user
resolved" from firing.
"""

from __future__ import annotations

from fastapi import Request

from prep.auth.anon_cookie import (
    COOKIE_NAME,
    mint_cookie,
    needs_refresh,
    verify_cookie,
)
from prep.auth.port import IdentityProvider, ResolvedUser, SignInUrls

ANON_PROVIDER_TAG = "anon"
ANON_DISPLAY_NAME = "Guest"


class AnonymousFallbackProvider(IdentityProvider):
    """Decorator over an `IdentityProvider` that adds cookie identity."""

    def __init__(self, inner: IdentityProvider) -> None:
        self._inner = inner

    @property
    def name(self) -> str:
        # Routes branch on the wrapped adapter's tag (e.g. the
        # clerk-only account flows), so the decorator is transparent.
        return self._inner.name

    def resolve(self, request: Request) -> ResolvedUser | None:
        resolved = self._inner.resolve(request)
        if resolved is not None:
            return resolved
        if self._inner.has_dormant_session(request):
            return None
        return self._resolve_anon_cookie(request)

    def urls(self) -> SignInUrls:
        return self._inner.urls()

    def has_dormant_session(self, request: Request) -> bool:
        return self._inner.has_dormant_session(request)

    def _resolve_anon_cookie(self, request: Request) -> ResolvedUser | None:
        raw = request.cookies.get(COOKIE_NAME)
        if not raw:
            return None
        cookie = verify_cookie(raw)
        if cookie is None:
            request.state.anon_cookie_stale = True
            return None
        if needs_refresh(cookie):
            request.state.anon_cookie_refresh = mint_cookie(cookie.external_id)
        return ResolvedUser(
            external_id=cookie.external_id,
            email=None,
            display_name=ANON_DISPLAY_NAME,
            profile_pic_url=None,
            provider=ANON_PROVIDER_TAG,
            is_anonymous=True,
        )

    def __getattr__(self, item: str):
        """Provider-specific extras (the Clerk SDK handle, the secret
        key) stay reachable through the decorator."""
        inner = self.__dict__.get("_inner")
        if inner is None:
            raise AttributeError(item)
        return getattr(inner, item)
