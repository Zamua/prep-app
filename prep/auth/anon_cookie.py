"""Signed cookie that names an anonymous account.

The cookie is the only credential an anonymous user has, so it is
signed rather than opaque: garbage is rejected before sqlite is
touched, the issue time is trustworthy enough to enforce expiry
without a read, and rotating the secret revokes every outstanding
anonymous session at once.

Value shape: `v1.<id>.<iat>.<sig>` where `id` is 16 random bytes as
unpadded base64url, `iat` is unix seconds, and `sig` is the first 16
bytes of HMAC-SHA256 over `v1.<id>.<iat>`, also unpadded base64url.
Those same 16 bytes are the account's external id in hex, prefixed
`anon:` -- one value, two encodings.

Without a secret there is no signing key, so anonymous accounts are
off: `mint_cookie` raises and `verify_cookie` returns None. An
unsigned cookie would let anyone name themselves any user id.
"""

from __future__ import annotations

import base64
import hmac
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import Request
from starlette.responses import Response

from prep.byok.crypto import MasterKeyError, load_master_from_env
from prep.infrastructure import clock

log = logging.getLogger(__name__)

COOKIE_NAME = "prep_anon"
COOKIE_VERSION = "v1"
COOKIE_SAMESITE = "lax"

ID_BYTES = 16
SIG_BYTES = 16
SECRET_BYTES = 32

MAX_AGE_SECONDS = 15552000  # 180 days
# A sixth of the window: a returning visitor re-mints at most monthly
# and keeps 150 days of slack before the hard reject. Any interval
# shorter than the gap must never expire, so this stays far below
# MAX_AGE_SECONDS.
REFRESH_AFTER_SECONDS = 2592000  # 30 days
FUTURE_SKEW_SECONDS = 60

EXTERNAL_ID_PREFIX = "anon:"

SECRET_ENV = "PREP_ANON_COOKIE_SECRET"
MASTER_ENV = "PREP_KEY_ENCRYPTION_SECRET"
_HKDF_INFO = b"prep-anon-cookie-v1"


class AnonCookieDisabled(RuntimeError):
    """Raised when a mint is attempted with no signing secret."""


@dataclass(frozen=True)
class AnonCookie:
    """A verified `prep_anon` value."""

    external_id: str  # "anon:" + 32 hex chars
    issued_at: int  # unix seconds


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(value: str, expect: int) -> bytes | None:
    pad = "=" * (-len(value) % 4)
    try:
        raw = base64.urlsafe_b64decode(value + pad)
    except (ValueError, TypeError):
        return None
    return raw if len(raw) == expect else None


@lru_cache(maxsize=4)
def _resolve_secret(explicit: str, master: str) -> bytes | None:
    """Keyed on the raw env values so a changed env is picked up and
    the HKDF still runs once per distinct key."""
    if explicit:
        try:
            key = bytes.fromhex(explicit)
        except ValueError:
            key = b""
        if len(key) != SECRET_BYTES:
            log.warning(
                "%s is not %d hex bytes; anonymous accounts disabled", SECRET_ENV, SECRET_BYTES
            )
            return None
        return key
    if not master:
        return None
    try:
        ikm = load_master_from_env(MASTER_ENV)
    except MasterKeyError:
        return None
    # One key, one purpose: the master key signs nothing directly, the
    # HKDF label is the domain separation.
    return HKDF(algorithm=hashes.SHA256(), length=SECRET_BYTES, salt=None, info=_HKDF_INFO).derive(
        ikm
    )


def cookie_secret() -> bytes | None:
    """The signing key, or None when anonymous accounts are off."""
    return _resolve_secret(
        (os.environ.get(SECRET_ENV) or "").strip(),
        (os.environ.get(MASTER_ENV) or "").strip(),
    )


def is_enabled() -> bool:
    return cookie_secret() is not None


def external_id_from_bytes(raw: bytes) -> str:
    return EXTERNAL_ID_PREFIX + raw.hex()


def _id_bytes(external_id: str) -> bytes:
    if not external_id.startswith(EXTERNAL_ID_PREFIX):
        raise ValueError(f"not an anonymous external id: {external_id!r}")
    raw = bytes.fromhex(external_id[len(EXTERNAL_ID_PREFIX) :])
    if len(raw) != ID_BYTES:
        raise ValueError(f"anonymous external id must carry {ID_BYTES} bytes")
    return raw


def _sign(secret: bytes, payload: str) -> str:
    return _b64e(hmac.new(secret, payload.encode("ascii"), sha256).digest()[:SIG_BYTES])


def mint_cookie(external_id: str, *, issued_at: int | None = None) -> str:
    """Build a signed value for `external_id`. Re-minting passes the
    SAME external id, so the account and every device stamp of it are
    untouched: only the expiry evidence is refreshed."""
    secret = cookie_secret()
    if secret is None:
        raise AnonCookieDisabled("no anonymous cookie secret; anonymous accounts are off")
    iat = int(issued_at if issued_at is not None else clock.unix())
    payload = f"{COOKIE_VERSION}.{_b64e(_id_bytes(external_id))}.{iat}"
    return f"{payload}.{_sign(secret, payload)}"


def verify_cookie(raw: str | None, *, now: int | None = None) -> AnonCookie | None:
    """Parse and authenticate a cookie value. A value that fails any
    check is treated as absent: a forged cookie is an unauthenticated
    request, not a 400."""
    secret = cookie_secret()
    if not raw or secret is None:
        return None
    # Starlette un-quotes `\OOO` octal escapes out of the wire value,
    # so an ASCII header can still arrive here as a non-ASCII string.
    # Every part of a valid value is ASCII, and both `_sign` and
    # `compare_digest` raise on anything else.
    if not raw.isascii():
        return None
    parts = raw.split(".")
    if len(parts) != 4:
        return None
    version, id_part, iat_part, sig = parts
    if version != COOKIE_VERSION:
        return None
    id_raw = _b64d(id_part, ID_BYTES)
    if id_raw is None:
        return None
    try:
        iat = int(iat_part)
    except ValueError:
        return None
    expected = _sign(secret, f"{version}.{id_part}.{iat_part}")
    if not hmac.compare_digest(expected.encode("ascii"), sig.encode("ascii")):
        return None
    ts = int(now if now is not None else clock.unix())
    if iat > ts + FUTURE_SKEW_SECONDS or iat < ts - MAX_AGE_SECONDS:
        return None
    return AnonCookie(external_id=external_id_from_bytes(id_raw), issued_at=iat)


def needs_refresh(cookie: AnonCookie, *, now: int | None = None) -> bool:
    """True once the signed `iat` is old enough to re-mint. Re-issuing
    Max-Age alone does not roll the window: `iat` is inside the signed
    value, so the same value always expires on the same day."""
    ts = int(now if now is not None else clock.unix())
    return ts - cookie.issued_at > REFRESH_AFTER_SECONDS


def _cookie_path(request: Request) -> str:
    return (request.scope.get("root_path") or "") + "/"


def set_cookie(response: Response, request: Request, value: str) -> None:
    """SameSite=Lax is load-bearing: the hosted sign-in flow returns
    by top-level navigation, and Strict would drop the cookie on the
    one request that first carries a provider session."""
    response.set_cookie(
        COOKIE_NAME,
        value,
        max_age=MAX_AGE_SECONDS,
        path=_cookie_path(request),
        httponly=True,
        secure=request.url.scheme == "https",
        samesite=COOKIE_SAMESITE,
    )


def issue_cookie(request: Request, response: Response, external_id: str) -> str:
    """Set a freshly minted cookie for a new account and drop any
    pending update recorded by the resolver: the new value supersedes
    both, and a stale-cookie delete emitted afterwards would erase the
    account the response just handed out. Returns the value set."""
    value = mint_cookie(external_id)
    request.state.anon_cookie_stale = False
    request.state.anon_cookie_refresh = None
    set_cookie(response, request, value)
    return value


def clear_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        COOKIE_NAME,
        path=_cookie_path(request),
        httponly=True,
        secure=request.url.scheme == "https",
        samesite=COOKIE_SAMESITE,
    )


def forget_cookie(request: Request, response: Response) -> None:
    """Clear the cookie and drop the pending refresh the resolver may
    have recorded on this same request, which the middleware would
    otherwise re-set on the way out."""
    request.state.anon_cookie_stale = False
    request.state.anon_cookie_refresh = None
    clear_cookie(response, request)


def emit_cookie_updates(request: Request, response: Response) -> None:
    """Apply what `resolve()` could only record: it has no response.

    Called from middleware for every response, not just text/html, or
    a JSON request never clears a dead cookie and never refreshes a
    live one."""
    if getattr(request.state, "anon_cookie_stale", False):
        clear_cookie(response, request)
    elif getattr(request.state, "anon_cookie_refresh", None):
        set_cookie(response, request, request.state.anon_cookie_refresh)
