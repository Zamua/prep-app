"""Golden HTML renderer: every template under the contexts in
`contexts.py`, written as `html/<template>@<context>.html`.

Rendering goes through the app's Jinja env (filters and the `icon`
global registered by `prep.app`), with the context-processor names
supplied explicitly and a fake `request` carrying an empty
`root_path`, so no server and no database are involved.

Beside every golden, `contexts/<stem>@<name>.json` holds the same
context as JSON (`to_jsonable`) plus `app_base`, the origin the fake
`request` carries, so another renderer produces its candidate from
exactly the input the golden came from and injects nothing itself.
"""

from __future__ import annotations

import os

from tests.parity.oracles import (
    PARITY_BUILD_ID,
    dump_json,
    pin_clock,
    to_jsonable,
    write_corpus,
)
from tests.parity.oracles.contexts import (
    DECK_DISPLAY,
    Ctx,
    all_contexts,
    base_context,
    fake_request,
)

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


def _plain(value):
    """Sets have no JSON shape; they become sorted lists."""
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(v) for v in value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def app_base() -> str:
    """The request origin as a plain field: what the worker supplies from
    the request where Jinja read `request.url`."""
    url = fake_request().url
    return f"{url.scheme}://{url.netloc}"


def context_json(entry: Ctx) -> str:
    context = _plain(base_context(**entry.base) | entry.context)
    return dump_json(
        {
            "template": entry.template,
            "context": to_jsonable(context, deck_display=DECK_DISPLAY) | {"app_base": app_base()},
        }
    )


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
            files[f"contexts/{name[:-5]}.json"] = context_json(entry)
            index.append({"template": entry.template, "context": entry.name, "file": name})
    files["index.json"] = dump_json(index)
    return files


def main() -> None:
    root = write_corpus(NAME, extract())
    print(f"wrote {root}")


if __name__ == "__main__":
    main()
