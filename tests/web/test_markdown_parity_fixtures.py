"""Python side of the card-prompt markdown parity pin.

The offline app ports the templates' | markdown filter to JS
(static/js/study/markdown.js) so offline card prompts render the same
markup the online pages get from mistune. Both sides run the SAME
fixture file in tests/fixtures/markdown/, so the port and the Python
truth cannot drift apart silently: this module pins the REAL
registered Jinja filter (prep/app.py's mistune config) to the
fixture's `expected` strings; the browser suite
(tests/e2e/test_markdown_parity.py) pins markdownHTML() to the same
cases, honoring `js_expected` where the twin deliberately diverges.

Fixture case shape: {"id": ..., "input": ..., "expected": ...
[, "js_expected": ..., "note": ...]}. `expected` is always the exact
mistune output. `js_expected` marks a documented JS-side divergence
(tables and non-http(s) link schemes stay literal text there) and
must carry a `note`.

Set PREP_REGEN_MARKDOWN_FIXTURES=1 to rewrite every `expected` from
the live filter before asserting (for intentional mistune upgrades).
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


def test_divergences_are_documented_and_real() -> None:
    """Every js_expected must differ from expected (a matching
    override would silently mask drift) and must explain itself."""
    for case in CASES:
        if "js_expected" in case:
            assert case["js_expected"] != case["expected"], case["id"]
            assert case.get("note"), case["id"]


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
