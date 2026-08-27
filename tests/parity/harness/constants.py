"""Shared parity constants."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

PARITY_NOW_ISO = "2026-03-14T15:00:00Z"
PARITY_NOW = datetime(2026, 3, 14, 15, 0, 0, tzinfo=timezone.utc)
PARITY_TZ = "America/New_York"
PARITY_USER = "parity@example.com"
PARITY_USER_NAME = "Parity"
PARITY_BUILD_ID = "ce11d0000000"
PARITY_INTERNAL_TOKEN = "parity-internal-token"
INTERNAL_TOKEN_ENV = "PARITY_INTERNAL_TOKEN"


def internal_token(override: str | None = None) -> str:
    """The seed credential of the target under test.

    A fleet holds its own secret, so the constant is only the local
    default: every caller that talks to a target (the seed endpoint and
    the identity headers the browser sends) has to resolve it the same
    way, or the run seeds one server and browses as a stranger.
    """
    return override or os.environ.get(INTERNAL_TOKEN_ENV) or PARITY_INTERNAL_TOKEN


SCHEMES = ("light", "dark")

REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDENS_ROOT = REPO_ROOT / "tests" / "parity" / "goldens"
ARTIFACTS_ROOT = REPO_ROOT / "artifacts" / "parity"
