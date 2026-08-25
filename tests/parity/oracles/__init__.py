"""Domain oracles: extractors that turn the Python implementation into
committed corpora under `tests/fixtures/parity/<name>/`.

Each extractor module exposes `NAME` and `extract() -> dict[str, str]`
(relative file path to text) and runs standalone as
`python -m tests.parity.oracles.<name>`, which writes the corpus.
`test_oracles.py` re-runs every extractor in memory and asserts the
committed corpus still matches, so a corpus cannot drift from Python
silently.

Every extractor runs under `pin_clock()`: `PREP_FAKE_NOW` is the
parity instant and the process clock is re-resolved from it.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from tests.parity.harness.constants import (  # noqa: F401  re-exported
    PARITY_BUILD_ID,
    PARITY_INTERNAL_TOKEN,
    PARITY_NOW,
    PARITY_NOW_ISO,
    PARITY_TZ,
    PARITY_USER,
    PARITY_USER_NAME,
    REPO_ROOT,
)

FIXTURES_ROOT = REPO_ROOT / "tests" / "fixtures" / "parity"

ENV_FAKE_NOW = "PREP_FAKE_NOW"


def corpus_dir(name: str) -> Path:
    return FIXTURES_ROOT / name


def dump_json(obj: object) -> str:
    """One canonical text shape for every corpus: stable key order,
    readable indentation, UTF-8 verbatim, trailing newline."""
    return json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=True) + "\n"


def write_corpus(name: str, files: dict[str, str]) -> Path:
    """Replace the corpus directory with exactly `files`."""
    root = corpus_dir(name)
    if root.exists():
        shutil.rmtree(root)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def read_corpus(name: str) -> dict[str, str]:
    root = corpus_dir(name)
    if not root.is_dir():
        return {}
    return {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


class ClockPin:
    """Handle returned by `pin_clock`: `set(at)` moves the pinned
    instant for lifecycle cases that need time to pass."""

    def __init__(self, at: datetime):
        self.at = at

    def set(self, at: datetime) -> None:
        self.at = at
        _apply_pin(at)

    def unix(self) -> int:
        return int(self.at.timestamp())


def _apply_pin(at: datetime) -> None:
    os.environ[ENV_FAKE_NOW] = at.isoformat().replace("+00:00", "Z")
    from prep.infrastructure import clock

    clock.set_clock(clock.FixedClock(at))


@contextmanager
def pin_clock(at: datetime = PARITY_NOW) -> Iterator[ClockPin]:
    """`PREP_FAKE_NOW` set to the parity instant, the process clock
    pinned to it, both restored on exit."""
    previous = os.environ.get(ENV_FAKE_NOW)
    pin = ClockPin(at)
    _apply_pin(at)
    try:
        yield pin
    finally:
        if previous is None:
            os.environ.pop(ENV_FAKE_NOW, None)
        else:
            os.environ[ENV_FAKE_NOW] = previous
        from prep.infrastructure import clock

        clock.reset_clock()
