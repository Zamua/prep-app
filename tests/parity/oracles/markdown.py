"""Markdown oracle: the shared `cases.json` inputs through the
`markdown` filter registered on the app's Jinja env, as
`{id, input, expected}` rows.

The corpus test also asserts equality with `cases.json`'s own
`expected`, so the two files cannot drift apart.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.parity.oracles import REPO_ROOT, dump_json, write_corpus

NAME = "markdown"
CASES_PATH = REPO_ROOT / "tests" / "fixtures" / "markdown" / "cases.json"


def load_cases() -> list[dict]:
    with open(CASES_PATH, encoding="utf-8") as f:
        return json.load(f)["cases"]


def render(text: str | None) -> str:
    import prep.app  # noqa: F401  registers the filter on templates.env
    from prep.web.templates import templates

    return str(templates.env.filters["markdown"](text))


def build(cases_path: Path = CASES_PATH) -> list[dict]:
    with open(cases_path, encoding="utf-8") as f:
        cases = json.load(f)["cases"]
    return [{"id": c["id"], "input": c["input"], "expected": render(c["input"])} for c in cases]


def extract() -> dict[str, str]:
    return {"corpus.json": dump_json({"cases": build()})}


def main() -> None:
    root = write_corpus(NAME, extract())
    print(f"wrote {root}")


if __name__ == "__main__":
    main()
