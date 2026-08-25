"""Shared parity constants (docs/PARITY-GATE.md section 0)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

PARITY_NOW_ISO = "2026-03-14T15:00:00Z"
PARITY_NOW = datetime(2026, 3, 14, 15, 0, 0, tzinfo=timezone.utc)
PARITY_TZ = "America/New_York"
PARITY_USER = "parity@example.com"
PARITY_USER_NAME = "Parity"
PARITY_BUILD_ID = "ce11d0000000"
PARITY_INTERNAL_TOKEN = "parity-internal-token"

SCHEMES = ("light", "dark")

REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDENS_ROOT = REPO_ROOT / "tests" / "parity" / "goldens"
ARTIFACTS_ROOT = REPO_ROOT / "artifacts" / "parity"
