"""Flow registry (docs/PARITY-GATE.md C5).

One module per flow under `tests/parity/flows/` registers with
`@flow(...)`. `covers` names templates (`landing.html`,
`partials/plan_progress.html`) and partial states
(`partials/plan_progress.html#computing`); `test_registry.py` checks
the names resolve.

Selection env: `PARITY_PHASE=n` runs phases `<= n` (unset runs no
pixel flow, keeping the plain suite browser-free), `PARITY_FLOWS`
globs names (comma separated), `PARITY_SCHEME` picks one scheme.
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests.parity.harness.constants import SCHEMES

PHASE_ENV = "PARITY_PHASE"
FLOWS_ENV = "PARITY_FLOWS"
SCHEME_ENV = "PARITY_SCHEME"


class FlowCtx:
    """What a flow's `steps` receive. `shot` numbers itself in call
    order; the sink decides where the bytes go (golden or compare)."""

    def __init__(
        self,
        *,
        page: Any,
        base_url: str,
        seed: Mapping[str, Any],
        llm: Any,
        scheme: str,
        sink: Callable[[str, str, Any], Path],
        jobs: Any = None,
    ):
        self.page = page
        self.base_url = base_url
        self.seed = seed
        self.llm = llm
        # The Temporal + worker stack behind a phase-4 target, when one runs.
        self.jobs = jobs
        self.scheme = scheme
        self._sink = sink
        self._n = 0
        self.shots: list[Path] = []

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def shot(self, label: str, *, after_swap: Any = None) -> Path:
        self._n += 1
        name = f"{self._n:02d}-{slug(label)}@{self.scheme}.png"
        path = self._sink(name, label, after_swap)
        self.shots.append(path)
        return path

    def expect_after_swap(self):
        from tests.parity.harness.capture import expect_after_swap

        return expect_after_swap(self.page)


@dataclass(frozen=True)
class Flow:
    name: str
    phase: int
    seed: str | None
    covers: tuple[str, ...]
    steps: Callable[[FlowCtx], None]
    service_workers: str = "block"
    schemes: tuple[str, ...] = SCHEMES
    anonymous: bool = False
    #: Whether a local target has to run Temporal and the Go worker for it.
    jobs: bool = False
    tags: tuple[str, ...] = field(default=())


_REGISTRY: dict[str, Flow] = {}
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "step"


def flow(
    name: str,
    *,
    phase: int,
    seed: str | None,
    covers: tuple[str, ...] | list[str],
    service_workers: str = "block",
    schemes: tuple[str, ...] = SCHEMES,
    anonymous: bool = False,
    jobs: bool = False,
):
    if not _NAME_RE.match(name):
        raise ValueError(f"flow name {name!r} must be a kebab-case slug")
    if service_workers not in ("block", "allow"):
        raise ValueError(f"service_workers must be block|allow, got {service_workers!r}")
    if name in _REGISTRY:
        raise ValueError(f"flow {name!r} registered twice")

    def register(fn: Callable[[FlowCtx], None]) -> Flow:
        f = Flow(
            name=name,
            phase=phase,
            seed=seed,
            covers=tuple(covers),
            steps=fn,
            service_workers=service_workers,
            schemes=tuple(schemes),
            anonymous=anonymous,
            jobs=jobs,
        )
        _REGISTRY[name] = f
        return f

    return register


def _load_modules() -> None:
    import tests.parity.flows  # noqa: F401  registers on import


def all_flows() -> tuple[Flow, ...]:
    _load_modules()
    return tuple(sorted(_REGISTRY.values(), key=lambda f: (f.phase, f.name)))


def get_flow(name: str) -> Flow:
    _load_modules()
    return _REGISTRY[name]


def phase_limit(env: Mapping[str, str] | None = None) -> int | None:
    env = os.environ if env is None else env
    raw = (env.get(PHASE_ENV) or "").strip()
    if not raw:
        return None
    if raw.lower() == "all":
        return 99
    return int(raw)


def flow_selected(f: Flow, env: Mapping[str, str] | None = None) -> str | None:
    """None when the flow runs; else the reason it is skipped."""
    env = os.environ if env is None else env
    limit = phase_limit(env)
    if limit is None:
        return f"set {PHASE_ENV} to run pixel flows"
    if f.phase > limit:
        return f"phase {f.phase} > {PHASE_ENV}={limit}"
    globs = [g.strip() for g in (env.get(FLOWS_ENV) or "").split(",") if g.strip()]
    if globs and not any(fnmatch.fnmatchcase(f.name, g) for g in globs):
        return f"{FLOWS_ENV}={env.get(FLOWS_ENV)} excludes {f.name}"
    return None


def scheme_selected(f: Flow, scheme: str, env: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    if scheme not in f.schemes:
        return f"{f.name} does not run in {scheme}"
    pick = (env.get(SCHEME_ENV) or "").strip()
    if pick and pick != scheme:
        return f"{SCHEME_ENV}={pick}"
    return None


def selected(env: Mapping[str, str] | None = None) -> list[tuple[Flow, str]]:
    out = []
    for f in all_flows():
        if flow_selected(f, env) is not None:
            continue
        for scheme in f.schemes:
            if scheme_selected(f, scheme, env) is None:
                out.append((f, scheme))
    return out
