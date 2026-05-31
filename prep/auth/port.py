"""Pure auth abstractions.

`IdentityProvider` is the seam that lets prep speak to any identity
backend (Tailscale headers on the mac mini, Clerk on the public VPS,
a fake in tests) without the rest of the app caring which is in use.

The shape is intentionally minimal — a `resolve(request)` for the
per-request user lookup, and a `urls()` for the small set of links
the UI needs to drive sign-in / sign-out / profile. Providers can
add backend-specific extras (e.g. Clerk's webhook receiver) outside
this Protocol; the routes that need them import the adapter
directly. The Protocol is for the broad call surface that every
provider must satisfy.

`ResolvedUser.external_id` is the universal primary key — opaque,
provider-supplied, used as the `users.tailscale_login` column value
in the DB (kept that column name for historical / migration-free
reasons). For TailscaleProvider it's the email; for ClerkProvider
it's the Clerk user_id; for FakeProvider it's whatever the test
sets. The downstream code never inspects its shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from fastapi import Request


@dataclass(frozen=True)
class ResolvedUser:
    """Provider-agnostic user shape returned by `IdentityProvider.resolve`."""

    # Stable opaque identifier — used as the row key in the users table
    # and as the FK target on every user-owned row. Persists across
    # email changes (when the provider supports that, e.g. Clerk).
    external_id: str
    email: str | None
    display_name: str | None
    profile_pic_url: str | None
    # Tag identifying which provider produced this user. Surfaced in
    # debug logs + the user-indicator chip; downstream code shouldn't
    # branch on it.
    provider: str


@dataclass(frozen=True)
class SignInUrls:
    """URLs the UI uses to drive sign-in / sign-out / profile.

    `None` for any field means "no such flow for this provider" —
    the template hides the corresponding control. TailscaleProvider
    returns all-None (sign-in is implicit via the proxy; sign-out
    isn't a thing). ClerkProvider returns real URLs."""

    sign_in: str | None
    sign_out: str | None
    account: str | None  # link to a hosted profile-management page


@runtime_checkable
class IdentityProvider(Protocol):
    """Resolves the user from an incoming HTTP request."""

    name: str  # short tag for logging + sanity checks

    def resolve(self, request: Request) -> ResolvedUser | None:
        """Return the resolved user, or None if unauthenticated.

        Implementations MUST NOT raise on missing auth — they return
        None and let the dependency layer translate that into a 401.
        Raise only for malformed-but-present credentials (e.g. a
        signed cookie that fails signature verification — that's a
        signal worth surfacing as a 400, not a silent 401)."""
        ...

    def urls(self) -> SignInUrls:
        """Return the (provider-specific) URLs for the sign-in flow."""
        ...


class AuthConfigError(RuntimeError):
    """Raised at boot when a provider is mis-configured (missing
    required env vars, invalid mode name, etc.). Caught by app.py to
    fail-fast with a readable message instead of crashing on the
    first request."""
