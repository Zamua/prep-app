"""Identity-provider registry — picks the adapter by env var.

`get_provider()` is the only callable consumers should reach for —
it caches one provider instance for the process. `set_provider()`
exists strictly for test injection (the FakeProvider path).

The provider classes live in sibling modules and are imported
lazily inside `_build_provider` so a deploy that only uses one
adapter (e.g. mac-mini on TailscaleProvider) never pays the import
cost of ClerkProvider's `clerk-backend-api` SDK + its httpx
plumbing. Same lazy pattern as prep.agent.sdk_adapter.
"""

from __future__ import annotations

import os

from prep.auth.port import AuthConfigError, IdentityProvider

# Process-wide cache. None on cold start; lazily filled by get_provider().
_provider: IdentityProvider | None = None


def _adapter_for_mode(mode: str) -> IdentityProvider:
    if mode == "tailscale":
        from prep.auth.providers.tailscale import TailscaleProvider

        return TailscaleProvider()
    if mode == "clerk":
        from prep.auth.providers.clerk import ClerkProvider

        return ClerkProvider()
    if mode == "fake":
        from prep.auth.providers.fake import FakeProvider

        return FakeProvider()
    raise AuthConfigError(f"unknown PREP_AUTH_MODE={mode!r}; valid: tailscale | clerk | fake")


def _build_provider() -> IdentityProvider:
    """Resolve PREP_AUTH_MODE to the right adapter. Default
    `tailscale` keeps the mac-mini install (where the env var is
    unset) on its historical behavior.

    The adapter is then wrapped in the anonymous-cookie fallback when
    a signing secret resolves. No secret means anonymous accounts are
    off and the adapter is returned bare."""
    mode = (os.environ.get("PREP_AUTH_MODE") or "tailscale").strip().lower()
    adapter = _adapter_for_mode(mode)

    from prep.auth.anon_cookie import is_enabled

    if not is_enabled():
        return adapter

    from prep.auth.providers.anon import AnonymousFallbackProvider

    return AnonymousFallbackProvider(adapter)


def get_provider() -> IdentityProvider:
    """Return the active identity provider (cached after first call)."""
    global _provider
    if _provider is None:
        _provider = _build_provider()
    return _provider


def set_provider(provider: IdentityProvider | None) -> None:
    """Override / clear the cached provider — TESTS ONLY.

    Passing None forces the next get_provider() call to re-read env
    and re-instantiate; passing a provider instance pins it directly.
    Lets tests bypass env-var manipulation when they need a specific
    Fake setup."""
    global _provider
    _provider = provider
