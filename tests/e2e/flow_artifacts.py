"""Screenshot recording for flow tests.

Deliberately independent of pytest-playwright's artifact machinery. This
suite defines its own `page` fixture, which shadows the plugin's, so the
plugin's `--screenshot` / `--video` / `--tracing` finalizers never run and
write nothing. Calling `shot()` explicitly sidesteps that entirely and also
records the passing path, which the plugin only does on failure.

Output goes to $PREP_E2E_ARTIFACTS (default `artifacts/` at the repo root),
one directory per flow, with a numeric prefix so the sequence reads in order:

    artifacts/anonymous-merge-geography/01-splash.png
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def artifacts_root() -> Path:
    return Path(os.environ.get("PREP_E2E_ARTIFACTS") or (_REPO_ROOT / "artifacts"))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "step"


class FlowRecorder:
    """Numbered screenshots for one flow, in call order."""

    def __init__(self, flow: str):
        self.dir = artifacts_root() / _slug(flow)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._n = 0
        self.shots: list[Path] = []

    def shot(self, page, label: str) -> Path:
        self._n += 1
        path = self.dir / f"{self._n:02d}-{_slug(label)}.png"
        # full_page so a short viewport does not silently crop the evidence.
        page.screenshot(path=str(path), full_page=True)
        self.shots.append(path)
        return path

    def manifest(self) -> Path:
        """A flat index, so a reviewer reads the flow without opening each file.

        Also the only record of what THIS run produced: shots overwrite by
        index and nothing clears the directory, so a run that takes fewer
        screenshots than the last leaves the older trailing files behind."""
        path = self.dir / "steps.txt"
        path.write_text("\n".join(p.name for p in self.shots) + "\n")
        return path
