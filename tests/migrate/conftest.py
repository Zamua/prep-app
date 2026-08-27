"""One synthetic snapshot for the whole migration suite.

Session-scoped: `migrate.synth` materialises the frozen schema, and
the exporter tests only ever read the file. Small counts, prod shape: the
roles the spec names (heavy user, mid-merge, PAT holder, subscription
row, both retention clamp ends, an anonymous account with no rows) are
all present at eight providers and six anonymous.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from migrate import synth

SEED = 7
NOW = datetime(2026, 8, 26, 14, 0, 0, tzinfo=timezone.utc)
USERS = 14
ANONYMOUS = 6
HEAVY_QUESTIONS = 30
HEAVY_REVIEWS = 90


@pytest.fixture(scope="session")
def fixture(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, synth.Plan]:
    out = tmp_path_factory.mktemp("snapshot") / "prep.snapshot.sqlite"
    plan = synth.generate(
        out,
        users=USERS,
        seed=SEED,
        anonymous=ANONYMOUS,
        heavy_questions=HEAVY_QUESTIONS,
        heavy_reviews=HEAVY_REVIEWS,
        now=NOW,
    )
    return out, plan


@pytest.fixture(scope="session")
def snapshot(fixture: tuple[Path, synth.Plan]) -> Path:
    return fixture[0]


@pytest.fixture(scope="session")
def plan(fixture: tuple[Path, synth.Plan]) -> synth.Plan:
    """The roles the generator decided on, so a test names one rather
    than re-deriving which user is which."""
    return fixture[1]


@pytest.fixture(scope="session")
def exported(snapshot: Path, tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict]:
    from migrate.export import export

    out = tmp_path_factory.mktemp("export")
    return out, export(snapshot, out, now=NOW)
