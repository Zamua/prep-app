"""The DOM gate for the TypeScript renderer: every golden under
`tests/fixtures/parity/html/` against the same context rendered by
`worker/build/render-fixtures.cjs` (nunjucks-slim, the shim, the view
models), compared with `dom_diff`.

Needs `cd worker && npm run build` first; the bundle renders every
`contexts/*.json` once per session into `artifacts/parity/ts-html/`.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.parity.dom_diff import dom_diff
from tests.parity.harness.constants import REPO_ROOT

HTML = REPO_ROOT / "tests" / "fixtures" / "parity" / "html"
CONTEXTS = HTML / "contexts"
BUNDLE = REPO_ROOT / "worker" / "build" / "render-fixtures.cjs"
OUT = REPO_ROOT / "artifacts" / "parity" / "ts-html"


def goldens() -> list[str]:
    return sorted(
        p.relative_to(HTML).as_posix() for p in HTML.rglob("*.html") if CONTEXTS not in p.parents
    )


@pytest.fixture(scope="session")
def rendered():
    assert BUNDLE.is_file(), f"{BUNDLE} is missing; run `npm run build` in worker/"
    assert CONTEXTS.is_dir(), f"{CONTEXTS} is missing"
    subprocess.run(["node", str(BUNDLE), str(CONTEXTS), str(OUT)], check=True, cwd=REPO_ROOT)
    return OUT


def test_every_golden_has_a_context():
    missing = [rel for rel in goldens() if not (CONTEXTS / rel).with_suffix(".json").is_file()]
    assert not missing, missing


@pytest.mark.parametrize("rel", goldens())
def test_dom_equivalent(rendered, rel: str):
    candidate_path = rendered / rel
    assert candidate_path.is_file(), f"the TypeScript renderer produced no {rel}"
    golden = (HTML / rel).read_text(encoding="utf-8")
    candidate = candidate_path.read_text(encoding="utf-8")
    diffs = dom_diff(golden, candidate)
    assert not diffs, "\n".join(str(d) for d in diffs[:20]) + (
        f"\n... {len(diffs) - 20} more" if len(diffs) > 20 else ""
    )
