"""AES-256-GCM envelope encryption for BYOK secrets.

Plaintext keys (sk-ant-api03-…) flow through here on the way to the
DB and back out on the way to the AI call. Nothing else in the
codebase touches the master key, and nothing else in the codebase
unwraps ciphertext — keep that invariant.

## Threat model + design choices

- **At rest, the DB stores ciphertext.** A stolen sqlite file or
  backup yields nothing without `PREP_KEY_ENCRYPTION_SECRET`. The
  master should be supplied via a 0600 .env file separate from the
  data volume, not committed alongside the application.
- **AES-256-GCM** is the standard authenticated-encryption choice.
  Confidentiality + integrity, single primitive, no padding oracle
  to worry about. `cryptography.hazmat.primitives.ciphers.aead.AESGCM`.
- **96-bit (12-byte) nonce, fresh per encryption.** GCM's safety
  margin is birthday-bound at ~2^32 messages per key with random
  nonces; we'll have nowhere near that on a prep-scale deploy. Nonce
  + ciphertext are concatenated and base64'd for storage as TEXT.
- **No AAD.** No request-bound metadata to bind to the ciphertext
  yet; if/when we add per-user master keys we'd bind user_id.
- **Failure mode = lockout, not silent fallback.** If the master key
  is missing or wrong, decryption raises and the caller fails the
  request. We DO NOT fall back to plaintext storage or skip the call;
  better that BYOK is broken loudly than that we leak by drifting
  into a non-encrypted state.

## Master key format

`PREP_KEY_ENCRYPTION_SECRET` is a hex-encoded 32-byte value:
    openssl rand -hex 32

Other formats (base64, raw bytes via env) are rejected with a clear
message so a misconfigured deploy fails fast at boot.

## Rotation

For v1 there is no online re-encryption — rotating the master means
re-encrypting every row in `byok_credentials` with the new key in
a one-shot migration script (not yet written; defer until needed).
Users can always delete + re-paste their key to migrate themselves.
"""

from __future__ import annotations

import base64
import os
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Production callers should always use load_master_from_env() so the
# error path stays consistent. The module-level helpers stay exposed
# for tests that supply their own key without env mutation.

NONCE_LEN = 12  # GCM standard
KEY_LEN = 32  # AES-256


class MasterKeyError(RuntimeError):
    """Raised when the master encryption key is missing or malformed.
    Boot fails closed on this — never silently downgrade to no-crypto."""


class DecryptionError(RuntimeError):
    """Wraps AES-GCM tag-mismatch failures. Surfaces as `Failed to
    decrypt credential — master key may have changed or ciphertext is
    corrupt` at the caller, never as the underlying primitive's error
    (which leaks too much detail in stack traces)."""


def load_master_from_env(env_var: str = "PREP_KEY_ENCRYPTION_SECRET") -> bytes:
    """Read + parse the master key from the environment.

    The value must be a hex-encoded 32-byte (256-bit) key. Anything
    else (missing, wrong length, non-hex) raises `MasterKeyError`
    with a clear remediation hint.
    """
    raw = (os.environ.get(env_var) or "").strip()
    if not raw:
        raise MasterKeyError(
            f"{env_var} is not set. Generate one with "
            "`openssl rand -hex 32` and put it in your prep.env (chmod 600)."
        )
    try:
        key = bytes.fromhex(raw)
    except ValueError as e:
        raise MasterKeyError(
            f"{env_var} must be hex-encoded (e.g. `openssl rand -hex 32`). "
            f"Got a non-hex value: {e}"
        ) from e
    if len(key) != KEY_LEN:
        raise MasterKeyError(
            f"{env_var} must decode to {KEY_LEN} bytes (256 bits). "
            f"Got {len(key)} bytes — regenerate with `openssl rand -hex {KEY_LEN}`."
        )
    return key


def encrypt(plaintext: str, master_key: bytes) -> str:
    """Encrypt a plaintext secret and return a storable string.

    Output shape: `base64(nonce || ciphertext+tag)`. ASCII-safe, fits
    in a SQLite TEXT column, decoded by `decrypt()` symmetrically.

    Empty plaintext is not allowed — we'd be encrypting nothing, and
    callers always have a real key to store.
    """
    if not plaintext:
        raise ValueError("cannot encrypt empty plaintext")
    if len(master_key) != KEY_LEN:
        raise MasterKeyError(f"master key must be {KEY_LEN} bytes, got {len(master_key)}")
    aesgcm = AESGCM(master_key)
    nonce = secrets.token_bytes(NONCE_LEN)
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), associated_data=None)
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt(blob: str, master_key: bytes) -> str:
    """Inverse of `encrypt()`. Raises `DecryptionError` on any failure
    (tag mismatch, malformed base64, truncated nonce). Callers should
    NOT catch + retry — a failure here means either the master rotated
    out from under us or the ciphertext was tampered with.
    """
    if not blob:
        raise DecryptionError("empty ciphertext blob")
    if len(master_key) != KEY_LEN:
        raise MasterKeyError(f"master key must be {KEY_LEN} bytes, got {len(master_key)}")
    try:
        raw = base64.b64decode(blob, validate=True)
    except (ValueError, base64.binascii.Error) as e:
        raise DecryptionError(f"ciphertext is not valid base64: {e}") from e
    if len(raw) < NONCE_LEN + 16:
        # 16 bytes is the minimum GCM tag length; anything shorter
        # can't possibly carry a nonce + tag + at least 0 bytes of CT.
        raise DecryptionError("ciphertext blob too short")
    nonce, ct = raw[:NONCE_LEN], raw[NONCE_LEN:]
    aesgcm = AESGCM(master_key)
    try:
        plain = aesgcm.decrypt(nonce, ct, associated_data=None)
    except InvalidTag as e:
        raise DecryptionError(
            "AES-GCM tag mismatch — master key may have rotated or " "ciphertext is corrupt."
        ) from e
    return plain.decode("utf-8")


def mask(secret: str, *, prefix_chars: int = 14, suffix_chars: int = 4) -> str:
    """Render a secret as `sk-ant-api03-…x9zT` for display.

    Defaults size the prefix to capture the standard Anthropic key
    prefix (`sk-ant-api03-` is 13 chars + 1 to disambiguate) and the
    last 4 chars so the user can verify they pasted the right key
    when reviewing the settings page. Adjust per-provider later if
    OpenRouter / others use a different shape.

    Never log/persist a fuller view than this. The whole point of the
    masking helper is that callers don't have to remember the rules.
    """
    secret = secret or ""
    if len(secret) <= prefix_chars + suffix_chars + 1:
        # Too short to redact meaningfully — return a placeholder
        # rather than a recognizable-shape fragment.
        return "…"
    return f"{secret[:prefix_chars]}…{secret[-suffix_chars:]}"
