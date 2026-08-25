"""Screenshot recording for flow tests.

Deliberately independent of pytest-playwright's artifact machinery. This
suite defines its own `page` fixture, which shadows the plugin's, so the
plugin's `--screenshot` / `--video` / `--tracing` finalizers never run and
write nothing. Calling `shot()` explicitly sidesteps that entirely and also
records the passing path, which the plugin only does on failure.

Output goes to $PREP_E2E_ARTIFACTS (default `artifacts/` at the repo root),
one directory per flow, with a numeric prefix so the sequence reads in order:

    artifacts/anonymous-merge-geography/01-splash.png
    artifacts/anonymous-merge-geography/meta.json
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Every surface rises over ~500ms and the card list staggers on top, so a
# screenshot taken the instant a navigation resolves catches a half-faded
# overlay. Infinite animations (spinners) are excluded or this never settles.
_ANIMATIONS_SETTLED = """() => {
  if (!document.getAnimations) return true;
  return document.getAnimations().filter((a) => {
    if (a.playState !== 'running') return false;
    const t = a.effect && a.effect.getComputedTiming();
    return !t || t.iterations !== Infinity;
  }).length === 0;
}"""


def artifacts_root() -> Path:
    return Path(os.environ.get("PREP_E2E_ARTIFACTS") or (_REPO_ROOT / "artifacts"))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "step"


def _current_nodeid() -> str:
    """pytest exports the running test; the report joins filmstrips to
    results on it, so a flow with no matching result is a bug worth seeing."""
    raw = os.environ.get("PYTEST_CURRENT_TEST", "")
    return raw.split(" (")[0].strip()


class FlowRecorder:
    """Numbered screenshots for one flow, in call order."""

    def __init__(self, flow: str, settle_ms: int = 2000):
        self.flow = flow
        self.dir = artifacts_root() / _slug(flow)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.settle_ms = settle_ms
        self._n = 0
        self.shots: list[Path] = []
        self._write_meta()

    def _write_meta(self) -> None:
        (self.dir / "meta.json").write_text(
            json.dumps(
                {
                    "flow": self.flow,
                    "nodeid": _current_nodeid(),
                    "shots": [p.name for p in self.shots],
                },
                indent=2,
            )
        )

    def settle(self, page) -> None:
        """Best effort: a screenshot is worth more than a failed wait, so a
        timeout here must never fail the test it is documenting."""
        try:
            page.wait_for_function(_ANIMATIONS_SETTLED, timeout=self.settle_ms)
        except Exception:  # noqa: BLE001 - waiting is an optimisation, not an assertion
            pass

    def shot(self, page, label: str, settle: bool = True) -> Path:
        if settle:
            self.settle(page)
        self._n += 1
        path = self.dir / f"{self._n:02d}-{_slug(label)}.png"
        # full_page so a short viewport does not silently crop the evidence.
        page.screenshot(path=str(path), full_page=True)
        self.shots.append(path)
        self._write_meta()
        return path

    def manifest(self) -> Path:
        """A flat index, so a reviewer reads the flow without opening each file.

        Shots are keyed by index and nothing clears the directory, so a shorter
        re-run leaves a longer one's trailing files behind.
        """
        path = self.dir / "steps.txt"
        path.write_text("\n".join(p.name for p in self.shots) + "\n")
        self._write_meta()
        return path
