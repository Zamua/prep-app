"""Tier 3's two schedulers, behind one port.

Bytes agreeing is necessary, not sufficient: a card can copy perfectly
and still schedule differently if the retention resolution changes. So
every migrated card is scheduled twice - `py-fsrs` from the snapshot row,
`worker/domain/fsrs` from the cell row - and the two answers are
compared.

Fuzz is off on both sides. A fuzzed interval is a random draw; comparing
two draws proves nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

VERDICTS: tuple[str, ...] = ("right", "wrong")

# The exception each side raises for the same refused state. py-fsrs
# asserts on a Relearning card with no step; the port throws a named
# error. A mismatch here is a real divergence, so the names are mapped
# rather than both being flattened to "error".
PY_ERROR_NAMES = {
    "AssertionError": "RelearningStepMissing",
    "ValueError": "InvalidCardState",
    "KeyError": "InvalidCardState",
}

# Batched so the node side holds one bounded job at a time, the way the
# import endpoint holds one chunk.
BATCH = 2000


class OracleUnavailable(RuntimeError):
    """Tier 3 cannot run. Never downgraded to a skip: a tier that did not
    run is not a tier that passed."""


@dataclass(frozen=True)
class ScheduleInput:
    """One card's FSRS state plus the retention that resolved for it."""

    key: str
    stability: float | None
    difficulty: float | None
    fsrs_state: int
    last_review: str | None
    retention: float | None

    def as_json(self) -> dict:
        return {
            "key": self.key,
            "stability": self.stability,
            "difficulty": self.difficulty,
            "fsrs_state": self.fsrs_state,
            "last_review": self.last_review,
            "retention": self.retention,
        }


class ScheduleOracle(Protocol):
    def schedule(self, cards: Sequence[ScheduleInput], now: str) -> dict[str, dict[str, dict]]: ...


# ---- the Python side ------------------------------------------------------


@contextmanager
def _no_fuzz():
    """py-fsrs fuzzes by default. Swapping the app's scheduler cache for
    one holding fuzz-disabled schedulers keeps `schedule_review` itself in
    the path, so the clamp, the rounding and the wrapper are still under
    test."""
    from fsrs import Scheduler

    import prep.domain.srs as srs

    class NoFuzz(Scheduler):
        def __init__(self, *args, **kwargs):
            kwargs["enable_fuzzing"] = False
            super().__init__(*args, **kwargs)

    saved_cache, saved_class = srs._SCHEDULER_CACHE, srs._FsrsScheduler
    srs._SCHEDULER_CACHE, srs._FsrsScheduler = {}, NoFuzz
    try:
        yield
    finally:
        srs._SCHEDULER_CACHE, srs._FsrsScheduler = saved_cache, saved_class


class PyFsrsOracle:
    """`prep.domain.srs.schedule_review`, the reference the port was
    written against."""

    def schedule(self, cards: Sequence[ScheduleInput], now: str) -> dict[str, dict[str, dict]]:
        from prep.domain.srs import CardSRSState, Verdict, schedule_review

        at = datetime.fromisoformat(now)
        out: dict[str, dict[str, dict]] = {}
        with _no_fuzz():
            for card in cards:
                per: dict[str, dict] = {}
                for verdict in VERDICTS:
                    state = CardSRSState(
                        stability=card.stability,
                        difficulty=card.difficulty,
                        fsrs_state=card.fsrs_state,
                        last_review=(
                            datetime.fromisoformat(card.last_review)
                            if card.last_review is not None
                            else None
                        ),
                    )
                    try:
                        r = schedule_review(
                            state,
                            Verdict.RIGHT if verdict == "right" else Verdict.WRONG,
                            now=at,
                            desired_retention=card.retention,
                        )
                    except Exception as e:  # noqa: BLE001 - the class is the comparison
                        name = type(e).__name__
                        per[verdict] = {"error": PY_ERROR_NAMES.get(name, name)}
                        continue
                    per[verdict] = {
                        "stability": r.state.stability,
                        "difficulty": r.state.difficulty,
                        "fsrs_state": r.state.fsrs_state,
                        "last_review": r.state.last_review.isoformat()
                        if r.state.last_review
                        else None,
                        "next_due": r.next_due.isoformat(),
                        "interval_seconds": r.interval_seconds,
                        "step_bucket": r.step_bucket,
                    }
                out[card.key] = per
        return out


# ---- the TypeScript side --------------------------------------------------


def worker_dir(repo: Path | None = None) -> Path:
    root = Path(repo) if repo is not None else Path(__file__).resolve().parent.parent.parent
    return root / "worker"


class NodeFsrsOracle:
    """`worker/domain/fsrs`, bundled once per run and driven over stdin.

    The bundle exists because the domain modules import each other without
    file extensions, which node's own resolver will not follow; esbuild's
    will, and it is already the repo's way of running domain TypeScript
    under node.
    """

    def __init__(self, worker: Path | None = None, *, node: str | None = None) -> None:
        self.worker = worker or worker_dir()
        self.node = node or shutil.which("node") or "node"
        self._bundle: Path | None = None
        self._tmp: tempfile.TemporaryDirectory | None = None

    def _entry(self) -> Path:
        entry = self.worker / "scripts" / "fsrs-oracle.mjs"
        if not entry.is_file():
            raise OracleUnavailable(f"{entry} is missing; tier 3 has no cell-side scheduler to run")
        return entry

    def bundle(self) -> Path:
        if self._bundle is not None:
            return self._bundle
        esbuild = self.worker / "node_modules" / ".bin" / "esbuild"
        if not esbuild.is_file():
            raise OracleUnavailable(
                f"{esbuild} is missing; run `npm install` in {self.worker} so tier 3 can bundle domain/fsrs"
            )
        self._tmp = tempfile.TemporaryDirectory(prefix="prep-fsrs-oracle-")
        out = Path(self._tmp.name) / "fsrs-oracle.mjs"
        result = subprocess.run(
            [
                str(esbuild),
                str(self._entry()),
                "--bundle",
                "--platform=node",
                "--format=esm",
                "--target=node20",
                f"--outfile={out}",
            ],
            capture_output=True,
            text=True,
            cwd=str(self.worker),
        )
        if result.returncode != 0:
            raise OracleUnavailable(
                f"esbuild failed to bundle the fsrs oracle: {result.stderr.strip()[:600]}"
            )
        self._bundle = out
        return out

    def schedule(self, cards: Sequence[ScheduleInput], now: str) -> dict[str, dict[str, dict]]:
        bundle = self.bundle()
        out: dict[str, dict[str, dict]] = {}
        for batch in _batched(cards, BATCH):
            job = json.dumps(
                {"now": now, "verdicts": list(VERDICTS), "cards": [c.as_json() for c in batch]}
            )
            result = subprocess.run(
                [self.node, str(bundle)],
                input=job,
                capture_output=True,
                text=True,
                cwd=str(self.worker),
                env={**os.environ, "NODE_OPTIONS": ""},
            )
            if result.returncode != 0:
                raise OracleUnavailable(
                    f"the node fsrs oracle exited {result.returncode}: {result.stderr.strip()[:600]}"
                )
            try:
                out.update(json.loads(result.stdout)["results"])
            except (ValueError, KeyError) as e:
                raise OracleUnavailable(
                    f"the node fsrs oracle answered unreadable output: {e}"
                ) from e
        return out


def _batched(items: Sequence[ScheduleInput], size: int) -> Iterable[Sequence[ScheduleInput]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
