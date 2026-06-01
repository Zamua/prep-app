"""Personal-access-token storage for the public API.

Tokens follow the hashed-at-rest pattern: we generate a random
`prep_pat_<base64url-32-bytes>` and persist sha256(token), NOT the
plaintext. The full token is shown to the user once at creation; from
then on only the masked `key_prefix` is recoverable for display.

sha256 (not argon2 / bcrypt) is deliberate — these tokens are 256-bit
random, so brute-forcing the hash is equivalent to brute-forcing the
underlying token directly. The CPU cost of a slow KDF buys nothing.
"""

from __future__ import annotations

import hashlib
import secrets

from prep.api.entities import TOKEN_PREFIX, ApiTokenMetadata
from prep.infrastructure.db import cursor, now


def _hash(token: str) -> str:
    """sha256 hex of the token string. Token is high-entropy random,
    so a slow KDF would be wasted CPU."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _mask(token: str) -> str:
    """Return the safe-for-display form of a token: `prep_pat_Aa…x9zT`.

    Keeps the prefix so a user can tell at a glance which token row
    matches the one they pasted into Claude / a curl script. The last
    4 chars are the disambiguator when a user has several tokens —
    matches the BYOK key-mask pattern in prep.byok.crypto.mask."""
    if not token or len(token) <= len(TOKEN_PREFIX) + 6:
        return "…"
    suffix = token[-4:]
    middle = token[len(TOKEN_PREFIX) : len(TOKEN_PREFIX) + 2]
    return f"{TOKEN_PREFIX}{middle}…{suffix}"


class ApiTokenRepo:
    """CRUD + lookup for api_tokens rows."""

    def issue(self, *, user_id: str, label: str | None = None) -> tuple[str, ApiTokenMetadata]:
        """Generate, persist, and return (plaintext_token, metadata).

        The plaintext is shown to the user exactly once; metadata can
        be re-rendered any time. Caller is responsible for surfacing
        the plaintext in a 'copy this once' UI.
        """
        random_part = secrets.token_urlsafe(32)
        token = f"{TOKEN_PREFIX}{random_part}"
        token_hash = _hash(token)
        key_prefix = _mask(token)
        ts = now()
        with cursor() as c:
            cur = c.execute(
                """
                INSERT INTO api_tokens
                    (user_id, token_hash, label, key_prefix, created_at, last_used_at)
                VALUES (?, ?, ?, ?, ?, NULL)
                """,
                (user_id, token_hash, label, key_prefix, ts),
            )
            row_id = cur.lastrowid
        return token, ApiTokenMetadata(
            id=row_id,
            user_id=user_id,
            label=label,
            key_prefix=key_prefix,
            created_at=ts,
            last_used_at=None,
        )

    def list_for_user(self, user_id: str) -> list[ApiTokenMetadata]:
        with cursor() as c:
            rows = c.execute(
                """
                SELECT id, user_id, label, key_prefix, created_at, last_used_at
                  FROM api_tokens
                 WHERE user_id = ?
                 ORDER BY created_at DESC
                """,
                (user_id,),
            ).fetchall()
        return [ApiTokenMetadata(**dict(r)) for r in rows]

    def delete(self, *, user_id: str, token_id: int) -> bool:
        """Revoke a token. Per-user scoped: a user can't delete
        someone else's token by guessing the id."""
        with cursor() as c:
            res = c.execute(
                "DELETE FROM api_tokens WHERE id = ? AND user_id = ?",
                (token_id, user_id),
            )
            return res.rowcount > 0

    def lookup(self, token: str) -> tuple[str, int] | None:
        """Return (user_id, token_id) for a presented bearer token, or
        None if the token doesn't match any row. Touches last_used_at
        as a side effect so the settings page shows useful staleness.

        Used by the FastAPI dep that gates /api/v1/* — it's the only
        caller and gets the user_id it needs to set on request.state.
        """
        token = (token or "").strip()
        if not token.startswith(TOKEN_PREFIX):
            return None
        token_hash = _hash(token)
        with cursor() as c:
            row = c.execute(
                "SELECT id, user_id FROM api_tokens WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if not row:
                return None
            c.execute(
                "UPDATE api_tokens SET last_used_at = ? WHERE id = ?",
                (now(), row["id"]),
            )
        return row["user_id"], row["id"]
