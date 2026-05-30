"""prep.agent.token_store — persist the Claude OAuth token under prep-data.

Replaces the prior agent-server-volume model. The token written by
`claude setup-token` lives in a single file under PREP_DATA_DIR
(default `/data/claude-oauth-token`). On app boot we read the file
and stamp `CLAUDE_CODE_OAUTH_TOKEN` into the process env so the
SDK adapter picks it up. The settings UI's connect/disconnect
routes call `write_token` / `delete_token` here; no HTTP roundtrip,
no separate container.

File permissions are 0600 — readable only by the prep container's
user. Atomic write (tmp + rename) so a crashed write never leaves
a partial token on disk.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Token filename inside PREP_DATA_DIR. Naming kept literal so an
# operator inspecting the volume immediately knows what it is.
_TOKEN_FILENAME = "claude-oauth-token"


def _data_dir() -> Path:
    """Resolve the directory the token file lives in. PREP_DATA_DIR
    overrides; otherwise /data (the compose volume mount)."""
    return Path((os.environ.get("PREP_DATA_DIR") or "/data").strip() or "/data")


def token_path() -> Path:
    return _data_dir() / _TOKEN_FILENAME


def token_exists() -> bool:
    p = token_path()
    return p.is_file() and p.stat().st_size > 0


def read_token() -> str | None:
    """Read the token from disk. Returns None if missing or empty.
    Strips surrounding whitespace so a trailing newline from a hand-
    edited file doesn't break the SDK auth."""
    p = token_path()
    if not p.is_file():
        return None
    try:
        raw = p.read_text(encoding="utf-8").strip()
    except OSError as e:
        logger.warning("could not read token file %s: %s", p, e)
        return None
    return raw or None


def write_token(token: str) -> None:
    """Atomically write the token to disk + permissions 0600. Caller
    is responsible for validating the token shape (the connect route
    rejects non `sk-ant-oat01-…` prefixes before reaching here)."""
    p = token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(token.strip() + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def delete_token() -> None:
    """Remove the token file. Idempotent — missing file is fine."""
    p = token_path()
    try:
        p.unlink()
    except FileNotFoundError:
        pass


def load_into_env() -> bool:
    """Read the token file (if present) and set CLAUDE_CODE_OAUTH_TOKEN
    in the process env so the SDK adapter can use it. Idempotent.

    Returns True iff the env var ended up set with a non-empty value
    (either we loaded it from the file, or it was already present).

    Called on app boot (prep.app) AND after /settings/agent/connect
    so a freshly-pasted token activates without a container restart.
    """
    existing = (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()
    if existing:
        return True
    token = read_token()
    if not token:
        return False
    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return True


def clear_env() -> None:
    """Unset CLAUDE_CODE_OAUTH_TOKEN so the next adapter call surfaces
    a clean AgentUnavailable. Called after /settings/agent/disconnect."""
    os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
