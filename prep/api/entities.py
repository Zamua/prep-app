"""Public-API token entities — pure value objects, no I/O."""

from __future__ import annotations

from dataclasses import dataclass

# Tokens look like `prep_pat_<base64url>` so they're easy to spot in
# logs and don't collide with Anthropic / OpenAI / OpenRouter shapes.
TOKEN_PREFIX = "prep_pat_"


@dataclass(frozen=True)
class ApiTokenMetadata:
    """Public-safe view of a stored API token — no secret material.

    `key_prefix` is the masked display form (e.g. `prep_pat_Aa…x9zT`).
    Safe for the settings table, the JSON API, log lines, anywhere.
    """

    id: int
    user_id: str
    label: str | None
    key_prefix: str
    created_at: str
    last_used_at: str | None
