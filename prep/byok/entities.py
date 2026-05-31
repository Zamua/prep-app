"""BYOK entities — pure value objects, no I/O.

Lives in the bounded context so callers depend on this module rather
than on the repo's row shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Provider(str, Enum):
    """AI providers supported via BYOK.

    Storing as TEXT in the DB (the enum value) so we can add OpenRouter
    etc. later without a migration. The Python enum exists for
    type-checking + exhaustive switch behavior.
    """

    ANTHROPIC_API = "anthropic-api"
    # Reserved for phase 3:
    # OPENROUTER = "openrouter"


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
