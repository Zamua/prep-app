"""Python side of the card-prompt markdown parity pin.

The card-prompt renderer is one TypeScript module (worker/domain/markdown)
whose browser twin static/js/study/markdown.js is generated from it, so
offline card prompts render the same markup the online pages get from
mistune. This module pins the REAL registered Jinja filter (prep/app.py's
mistune config) to the fixture's `expected` strings; the same cases form
the tests/fixtures/parity/markdown corpus the TypeScript side is held
byte-equal to, and the browser suite (tests/e2e/test_markdown_parity.py)
runs the twin on them.

Fixture case shape: {"id": ..., "input": ..., "expected": ...}.
`expected` is always the exact mistune output.

Set PREP_REGEN_MARKDOWN_FIXTURES=1 to rewrite every `expected` from the
live filter before asserting (for intentional mistune upgrades).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

import prep.app  # noqa: F401  registers the filter on templates.env
from prep.web.templates import templates

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "markdown" / "cases.json"


def _render(text: str) -> str:
    """The truth being pinned: the filter registered on the app's
    Jinja env, not a re-created mistune instance."""
    return str(templates.env.filters["markdown"](text))


def _load() -> dict[str, Any]:
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


def _regen(fixture: dict[str, Any]) -> None:
    for case in fixture["cases"]:
        case["expected"] = _render(case["input"])
    with open(FIXTURE_PATH, "w", encoding="utf-8") as f:
        json.dump(fixture, f, indent=1, ensure_ascii=False)
        f.write("\n")


FIXTURE = _load()
if os.environ.get("PREP_REGEN_MARKDOWN_FIXTURES") == "1":
    _regen(FIXTURE)
CASES = FIXTURE["cases"]


@pytest.mark.parametrize("case", [pytest.param(c, id=c["id"]) for c in CASES])
def test_mistune_output_pinned(case: dict[str, Any]) -> None:
    assert _render(case["input"]) == case["expected"]


def test_none_renders_empty() -> None:
    """The filter's None guard, which the fixture's empty-string case
    cannot cover (JSON has no undefined)."""
    assert _render(None) == ""


# ---- fixture integrity -------------------------------------------------
#
# The parametrized test sources its cases FROM the fixture file, so an
# empty or mistyped fixture would pass vacuously. These keep the pin
# falsifiable.


def test_fixture_counts_and_unique_ids() -> None:
    assert len(CASES) >= 25
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids))
    assert all(isinstance(c["input"], str) and isinstance(c["expected"], str) for c in CASES)


def test_no_case_carries_a_js_divergence() -> None:
    """The twin renders every case to `expected`; a per-side override
    would mask drift."""
    for case in CASES:
        assert "js_expected" not in case, case["id"]


def test_xss_cases_pin_escaping() -> None:
    """The fixture must keep exercising escape=True: raw HTML input
    comes out entity-escaped, never as markup."""
    by_id = {c["id"]: c for c in CASES}
    script = by_id["xss-script-tag"]
    img = by_id["xss-img-onerror"]
    assert "<script" not in script["expected"]
    assert "&lt;script&gt;" in script["expected"]
    assert "<img" not in img["expected"]
    assert "&lt;img" in img["expected"]
