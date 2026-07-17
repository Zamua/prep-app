"""Tailscale identity provider.

Reads the `Tailscale-User-Login` / `-Name` / `-Profile-Pic` headers
injected by `tailscale serve` when fronting the deploy. This is the
historical (and still default) auth path — Mac mini install uses it,
no env var needed.

Local dev / single-user deploys can set `PREP_DEFAULT_USER` to fake
the login header. Never set it in a multi-user deploy — every
header-less request becomes that user.
"""

from __future__ import annotations

import os

from fastapi import Request

from prep.auth.port import IdentityProvider, ResolvedUser, SignInUrls


class TailscaleProvider(IdentityProvider):
    """Resolves identity from Tailscale-injected headers."""

    name = "tailscale"

    def resolve(self, request: Request) -> ResolvedUser | None:
        login = request.headers.get("tailscale-user-login")
        if not login:
            fallback = (os.environ.get("PREP_DEFAULT_USER") or "").strip()
            login = fallback or None
        if not login:
            return None
        login = login.strip()
        display_name = request.headers.get("tailscale-user-name") or login.split("@", 1)[0]
        profile_pic = request.headers.get("tailscale-user-profile-pic") or None
        return ResolvedUser(
            external_id=login,
            email=login,
            display_name=display_name,
            profile_pic_url=profile_pic,
            provider=self.name,
        )

    def has_dormant_session(self, request: Request) -> bool:
        # Header-injected identity never expires between requests --
        # there is no "stale token, returning user" state to detect.
        return False

    def urls(self) -> SignInUrls:
        # Tailscale auth is implicit (you're either on the tailnet or
        # not). No sign-in/out UI to point at — the template uses None
        # to hide sign-in/out chrome on this deploy.
        return SignInUrls(sign_in=None, sign_out=None, account=None)
