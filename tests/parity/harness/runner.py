"""One (flow, scheme) run in golden or compare mode (docs/PARITY-GATE.md C7)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from tests.parity.harness import capture
from tests.parity.harness.compare import Report, compare
from tests.parity.harness.constants import ARTIFACTS_ROOT, GOLDENS_ROOT, PARITY_USER
from tests.parity.harness.contextspec import new_context, new_page
from tests.parity.harness.registry import Flow, FlowCtx
from tests.parity.harness.server import ParityTarget

MODE_ENV = "PARITY_MODE"


def mode() -> str:
    m = (os.environ.get(MODE_ENV) or "compare").strip().lower()
    if m not in ("golden", "compare"):
        raise ValueError(f"{MODE_ENV} must be golden|compare, got {m!r}")
    return m


@dataclass
class RunResult:
    flow: str
    scheme: str
    mode: str
    shots: list[Path] = field(default_factory=list)
    reports: dict[str, Report] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


def run_flow(
    flow: Flow,
    scheme: str,
    *,
    browser,
    target: ParityTarget,
    llm,
    goldens_root: Path = GOLDENS_ROOT,
    artifacts_root: Path = ARTIFACTS_ROOT,
    record_property=None,
) -> RunResult:
    run_mode = mode()
    result = RunResult(flow.name, scheme, run_mode)
    seed_response = target.seed(PARITY_USER, flow.seed) if flow.seed else {}

    golden_dir = goldens_root / flow.name
    out_dir = artifacts_root / flow.name

    def sink(name: str, label: str, after_swap) -> Path:
        golden = golden_dir / name
        if run_mode == "golden":
            capture.shot(page, golden, after_swap=after_swap)
            result.shots.append(golden)
            return golden
        candidate = out_dir / name
        capture.shot(page, candidate, after_swap=after_swap)
        result.shots.append(candidate)
        if not golden.exists():
            result.failures.append(f"{name}: no golden at {golden} (run with {MODE_ENV}=golden)")
            return candidate
        diff = out_dir / name.replace(".png", ".diff.png")
        report = compare(golden, candidate, diff)
        result.reports[name] = report
        if not report.passed:
            result.failures.append(f"{name}: {report.summary()}")
            if record_property is not None:
                record_property(f"diff:{name}", str(diff))
        return candidate

    ctx = new_context(
        browser,
        scheme,
        base_url=target.base_url,
        service_workers=flow.service_workers,
        identity=None if flow.anonymous else PARITY_USER,
    )
    try:
        page = new_page(ctx)
        fctx = FlowCtx(
            page=page,
            base_url=target.base_url,
            seed=seed_response,
            llm=llm,
            scheme=scheme,
            sink=sink,
        )
        flow.steps(fctx)
    finally:
        ctx.close()
    return result
