"""BYOK entities — pure value objects, no I/O.

Lives in the bounded context so callers depend on this module rather
than on the repo's row shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Provider(str, Enum):
    """AI providers supported via BYOK.

    Storing as TEXT in the DB (the enum value) so we can add more
    providers later without a migration. The Python enum exists for
    type-checking + exhaustive switch behavior. New values get a
    `ProviderInfo` entry below; nothing else in the codebase reaches
    for provider knowledge by enum value directly.
    """

    ANTHROPIC_API = "anthropic-api"
    OPENAI_API = "openai-api"
    OPENROUTER_API = "openrouter-api"
    # Retired 2026-08-25. The Claude subscription token is a Claude
    # Code credential: the Messages API rejects it and the only
    # sanctioned path runs the agent harness as a subprocess, which
    # this deploy does not host. Rows are dropped at the celld
    # cutover; the value stays reserved so a stored row still parses.
    CLAUDE_SUBSCRIPTION = "claude-subscription"


@dataclass(frozen=True)
class ProviderInfo:
    """Static metadata about a BYOK provider — display labels, accepted
    key shapes, console URL, default model. Lives outside the adapter
    so settings routes and templates can render the section without
    importing httpx-heavy adapter modules at template-render time.

    `key_prefixes` is a tuple of accepted prefixes (case-sensitive).
    A key must start with one of them to be accepted by the /connect
    route. The first entry is the canonical/displayed one in error
    messages.
    """

    provider: Provider
    label: str  # e.g. "Anthropic"
    short_label: str  # e.g. "anthropic" — for short-form copy
    key_prefixes: tuple[str, ...]
    console_url: str
    default_model: str


# PROVIDERS insertion order = display order on /settings/agent.
# API keys only: the retired subscription provider has no entry, so it
# renders nowhere and no new row can be created for it.
PROVIDERS: dict[Provider, ProviderInfo] = {
    Provider.ANTHROPIC_API: ProviderInfo(
        provider=Provider.ANTHROPIC_API,
        label="Anthropic",
        short_label="anthropic",
        key_prefixes=("sk-ant-api03-",),
        console_url="https://console.anthropic.com/settings/keys",
        default_model="claude-sonnet-4-6",
    ),
    Provider.OPENAI_API: ProviderInfo(
        provider=Provider.OPENAI_API,
        label="OpenAI",
        short_label="openai",
        # OpenAI emits several key shapes (project / service-account /
        # plain). All begin with `sk-`; we reject prefixes claimed by
        # other providers (sk-ant, sk-or) at the route layer instead
        # of trying to enumerate every legitimate OpenAI shape here.
        key_prefixes=("sk-",),
        console_url="https://platform.openai.com/api-keys",
        default_model="gpt-5-mini",
    ),
    Provider.OPENROUTER_API: ProviderInfo(
        provider=Provider.OPENROUTER_API,
        label="OpenRouter",
        short_label="openrouter",
        key_prefixes=("sk-or-v1-",),
        console_url="https://openrouter.ai/keys",
        # OpenRouter routes by `<vendor>/<model>` — pick a sane sonnet
        # default. The user can swap to gpt-5, gemini-2.5-pro, etc.
        # later via the model selector (#302).
        default_model="anthropic/claude-sonnet-4.5",
    ),
}


def provider_for_key(secret: str) -> Provider | None:
    """Return the provider whose key-prefix matches `secret`, or None.

    Disambiguates the OpenAI broad `sk-` prefix from Anthropic's
    `sk-ant-` and OpenRouter's `sk-or-`, and the Anthropic API key
    prefix from the Claude subscription OAuth prefix (both start with
    `sk-ant-`). Most-specific prefix wins."""
    secret = (secret or "").strip()
    if not secret:
        return None
    # An `sk-ant-oat01-` subscription token matches no provider now, so
    # it is refused at the form rather than stored as an API key.
    for p in (
        Provider.ANTHROPIC_API,
        Provider.OPENROUTER_API,
        Provider.OPENAI_API,
    ):
        for prefix in PROVIDERS[p].key_prefixes:
            if secret.startswith(prefix):
                # Belt-and-suspenders: don't classify a `sk-ant-` key
                # as OpenAI even though it technically starts with "sk-".
                if p is Provider.OPENAI_API and (
                    secret.startswith("sk-ant-") or secret.startswith("sk-or-")
                ):
                    continue
                return p
    return None


@dataclass(frozen=True)
class CredentialMetadata:
    """Public-facing view of a stored credential — no secret material.

    `key_prefix` shows ~prefix and last-4 chars (e.g.
    `sk-ant-api03-…x9zT`). Safe to render in HTML / log / API response.
    `created_at` / `last_used_at` are ISO-8601 UTC strings (same shape
    as the rest of the prep DB)."""

    user_id: int
    provider: Provider
    key_prefix: str
    created_at: str
    last_used_at: str | None
