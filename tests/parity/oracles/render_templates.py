"""Golden HTML renderer: every template under the contexts in
`contexts.py`, written as `html/<template>@<context>.html`.

Rendering goes through the app's Jinja env (filters and the `icon`
global registered by `prep.app`), with the context-processor names
supplied explicitly and a fake `request` carrying an empty
`root_path`, so no server and no database are involved.
"""

from __future__ import annotations

import os

from tests.parity.oracles import PARITY_BUILD_ID, dump_json, pin_clock, write_corpus
from tests.parity.oracles.contexts import Ctx, all_contexts, base_context, fake_request

NAME = "html"


def _env():
    os.environ.setdefault("TEMPORAL_HOST_PORT", "127.0.0.1:0")
    os.environ.setdefault("PREP_BUILD_ID", PARITY_BUILD_ID)
    import prep.app  # noqa: F401  registers filters and the icon global
    from prep.web.templates import templates

    return templates.env


def render(env, entry: Ctx) -> str:
    context = base_context(**entry.base) | entry.context
    context.setdefault("request", fake_request())
    return env.get_template(entry.template).render(context)


def output_name(entry: Ctx) -> str:
    stem = entry.template[:-5] if entry.template.endswith(".html") else entry.template
    return f"{stem}@{entry.name}.html"


def extract() -> dict[str, str]:
    env = _env()
    files: dict[str, str] = {}
    index: list[dict] = []
    with pin_clock():
        for entry in all_contexts():
            name = output_name(entry)
            assert name not in files, f"duplicate context {name}"
            files[name] = render(env, entry)
            index.append({"template": entry.template, "context": entry.name, "file": name})
    files["index.json"] = dump_json(index)
    return files


def main() -> None:
    root = write_corpus(NAME, extract())
    print(f"wrote {root}")


if __name__ == "__main__":
    main()
