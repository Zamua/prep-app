"""BYOKRepo — encrypt-on-write, decrypt-on-read access to the
`byok_credentials` table.

The repo is the ONLY place in the codebase that touches the master
key (besides one bootstrap probe in crypto.py for fail-fast). Callers
above this layer (settings routes, agent selection) never see
ciphertext and never see the master key. Tests inject a master via
the constructor so they don't have to mutate env.

Encryption / decryption errors propagate as `crypto.MasterKeyError`
or `crypto.DecryptionError` — we deliberately do NOT swallow them
into "no key for this user" because that would mask a config
problem as a missing-feature problem.
"""

from __future__ import annotations

from prep.byok import crypto
from prep.byok.entities import CredentialMetadata, Provider
from prep.infrastructure.db import cursor, now


class BYOKRepo:
    """Stores + retrieves per-user provider credentials.

    The master key is loaded once at construction and reused for all
    operations on this instance. Callers should treat a BYOKRepo as
    request-scoped (FastAPI dependency); the singleton SDK adapter
    holds its own.
    """

    def __init__(self, master_key: bytes | None = None):
        self._master = master_key if master_key is not None else crypto.load_master_from_env()

    # ---- write side ------------------------------------------------------

    def store(self, *, user_id: str, provider: Provider, secret: str) -> CredentialMetadata:
        """Upsert the user's credential for `provider`.

        Encrypts `secret` with the deploy master key, persists the
        ciphertext + a public-safe prefix for display, and returns
        the metadata view (no secret material in the return value).

        Replaces any existing row for (user_id, provider) — keys are
        meant to be rotated by paste-and-replace.
        """
        secret = (secret or "").strip()
        if not secret:
            raise ValueError("cannot store an empty secret")
        ciphertext = crypto.encrypt(secret, self._master)
        prefix = crypto.mask(secret)
        ts = now()
        with cursor() as c:
            c.execute(
                """
                INSERT INTO byok_credentials
                    (user_id, provider, ciphertext, key_prefix, created_at, last_used_at)
                VALUES (?, ?, ?, ?, ?, NULL)
                ON CONFLICT (user_id, provider) DO UPDATE SET
                    ciphertext   = excluded.ciphertext,
                    key_prefix   = excluded.key_prefix,
                    created_at   = excluded.created_at,
                    last_used_at = NULL
                """,
                (user_id, provider.value, ciphertext, prefix, ts),
            )
        return CredentialMetadata(
            user_id=0,  # only the surface-level routes care about display;
            # we don't surface DB ids here (user_id is the FK string).
            provider=provider,
            key_prefix=prefix,
            created_at=ts,
            last_used_at=None,
        )

    def delete(self, *, user_id: str, provider: Provider) -> bool:
        """Remove the user's credential for `provider`.

        Returns True if a row was removed, False if there was nothing
        to remove. Either way the user ends up in the "no key" state
        — callers shouldn't error on "already gone."
        """
        with cursor() as c:
            res = c.execute(
                "DELETE FROM byok_credentials WHERE user_id=? AND provider=?",
                (user_id, provider.value),
            )
            return res.rowcount > 0

    def touch_last_used(self, *, user_id: str, provider: Provider) -> None:
        """Update `last_used_at` to now. Called by the agent layer right
        AFTER a successful API call — so the settings page can show
        the user when the key was last actively useful (signal that
        BYOK is wired up correctly)."""
        with cursor() as c:
            c.execute(
                "UPDATE byok_credentials SET last_used_at=? WHERE user_id=? AND provider=?",
                (now(), user_id, provider.value),
            )

    # ---- read side -------------------------------------------------------

    def get_secret(self, *, user_id: str, provider: Provider) -> str | None:
        """Return the decrypted secret, or None if the user has no key
        for this provider. Crypto errors propagate."""
        with cursor() as c:
            row = c.execute(
                "SELECT ciphertext FROM byok_credentials WHERE user_id=? AND provider=?",
                (user_id, provider.value),
            ).fetchone()
        if not row:
            return None
        return crypto.decrypt(row["ciphertext"], self._master)

    def metadata(self, *, user_id: str, provider: Provider) -> CredentialMetadata | None:
        """Public-safe view — never touches the ciphertext or master.

        This is the right read for any settings / status surface;
        only the agent invocation path needs `get_secret`.
        """
        with cursor() as c:
            row = c.execute(
                """
                SELECT key_prefix, created_at, last_used_at
                FROM byok_credentials WHERE user_id=? AND provider=?
                """,
                (user_id, provider.value),
            ).fetchone()
        if not row:
            return None
        return CredentialMetadata(
            user_id=0,
            provider=provider,
            key_prefix=row["key_prefix"],
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
        )
