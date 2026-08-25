"""Page-context corpus: what the Python routes pass to their templates
for the phase 1 profiles, one file per request, so the TypeScript
cells can serve the same pages from fixtures before they have
repositories (docs/PHASE-1.md A7).

Each profile is seeded through `prep.dev.parity_seed.seed` and driven
in order through the scratch app with the contextspec headers;
`jinja2.Template.render` is hooked to record `(template, context)`.
A request may set flags (`sets`) that later pages depend on; a page
recorded under a flag carries `@<flag>` in its name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from tests.parity.flows.settings import BYOK_KEY
from tests.parity.oracles import PARITY_USER, dump_json, to_jsonable, write_corpus
from tests.parity.oracles.harness import scratch_app

NAME = "pages"

# Values a deployment mints for itself; pinned so the corpus is stable.
VOLATILE_KEYS = ("vapid_key",)
VOLATILE_MARK = "<VOLATILE>"


@dataclass(frozen=True)
class Req:
    method: str
    path: str
    data: dict | None = None
    sets: tuple[str, ...] = ()
    state: str | None = None
    ids: dict = field(default_factory=dict, compare=False)


def anonymous_requests() -> list[Req]:
    return [
        Req("GET", "/"),
        Req("GET", "/privacy"),
        Req("GET", "/_parity/reauth"),
        Req("GET", "/_parity/sign-out"),
        Req("GET", "/no-such-page-parity"),
        Req("GET", "/_parity/raise"),
        Req("GET", "/_parity/raise?status=429"),
    ]


def empty_requests(ids: dict) -> list[Req]:
    return [
        Req("GET", "/"),
        Req("GET", "/api/dashboard/deck-menus"),
        Req("GET", "/api/active-workflows-badge"),
    ]


def reader_requests(ids: dict) -> list[Req]:
    decks = ids["decks"]
    code_qid = ids["questions"]["srs_a"]["code"]
    srs_a = decks["srs_a"]["slug"]
    return [
        Req("GET", "/"),
        Req("GET", "/api/dashboard/deck-menus"),
        Req("GET", "/api/active-workflows-badge"),
        Req("GET", f"/deck/{srs_a}"),
        Req("POST", f"/deck/{srs_a}/pin", data={"pinned": "on"}, sets=("pinned",)),
        Req("GET", f"/deck/{srs_a}", state="pinned"),
        Req("GET", f"/deck/{decks['trivia']['slug']}"),
        Req("GET", f"/deck/{decks['empty']['slug']}"),
        Req("GET", "/decks/new"),
        Req("GET", "/decks/new/srs"),
        Req("GET", "/decks/new/trivia"),
        Req("GET", f"/deck/{srs_a}/question/new"),
        Req("GET", f"/question/{code_qid}/edit"),
        Req("GET", "/settings/agent"),
        Req(
            "POST",
            "/settings/agent/byok/anthropic-api/connect",
            data={"api_key": BYOK_KEY},
            sets=("byok",),
        ),
        Req("GET", "/settings/agent", state="byok"),
        Req("GET", "/settings/srs"),
        Req("GET", "/settings/editor"),
        Req("GET", "/settings/api"),
        Req("GET", "/notify"),
        Req("GET", "/notify/log"),
    ]


PROFILES = {
    "anonymous": (None, lambda ids: anonymous_requests()),
    "empty": ("empty", empty_requests),
    "reader": ("reader", reader_requests),
}


def path_slug(path: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", path.strip("/")).strip("-")
    return slug or "root"


def file_name(index: int, req: Req) -> str:
    name = f"{index:02d}-{req.method}-{path_slug(req.path)}"
    if req.state:
        name += f"@{req.state}"
    return f"{name}.json"


class RenderSpy:
    """Records every `(template, context)` `jinja2.Template.render`
    receives while active; a response is expected to render at most
    one top-level template (includes and macros do not call it)."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __enter__(self):
        import jinja2

        spy = self
        original = jinja2.Template.render

        def render(template, *args, **kwargs):
            spy.calls.append((template.name, dict(*args, **kwargs)))
            return original(template, *args, **kwargs)

        self._original = original
        jinja2.Template.render = render
        return self

    def __exit__(self, *exc):
        import jinja2

        jinja2.Template.render = self._original


def deck_display_map(ids: dict) -> dict[str, str]:
    return {d["slug"]: d["display"] for d in ids.get("decks", {}).values()}


def record(h, req: Req, *, headers: dict, deck_display: dict[str, str]) -> dict:
    with RenderSpy() as spy:
        response = h.client.request(req.method, req.path, headers=headers, data=req.data)
    content_type = response.headers.get("content-type", "")
    page: dict = {
        "method": req.method,
        "path": req.path,
        "status": response.status_code,
        "headers": {"content-type": content_type},
        "sets": list(req.sets),
    }
    location = response.headers.get("location")
    if location is not None:
        page["headers"]["location"] = location
    if spy.calls:
        assert len(spy.calls) == 1, f"{req.method} {req.path}: {len(spy.calls)} renders"
        template, context = spy.calls[0]
        page["template"] = template
        page["context"] = to_jsonable(context, deck_display=deck_display)
        for key in VOLATILE_KEYS:
            if key in page["context"]:
                page["context"][key] = VOLATILE_MARK
    else:
        page["body"] = response.text
    return page


def mount_parity_routes(app) -> None:
    """`prep.app` mounts the `/_parity/*` routes at import under the
    flag; a process that imported it earlier without the flag (pytest)
    gets them here, once."""
    from prep.dev import parity_seed
    from prep.web.parity import router

    if not any(getattr(r, "path", None) == "/_parity/raise" for r in app.routes):
        app.include_router(router)
    parity_seed.register(app)


def extract() -> dict[str, str]:
    files: dict[str, str] = {}
    with scratch_app(raise_server_exceptions=False) as h:
        from prep.dev.parity_seed import seed

        mount_parity_routes(h.client.app)

        for profile, (seed_profile, build) in PROFILES.items():
            ids = seed(PARITY_USER, seed_profile) if seed_profile else {}
            headers = h.headers() if seed_profile else {}
            display = deck_display_map(ids)
            files[f"{profile}/seed.json"] = dump_json(to_jsonable(ids))
            for i, req in enumerate(build(ids), start=1):
                page = record(h, req, headers=headers, deck_display=display)
                if not req.path.startswith("/_parity/raise"):
                    assert page["status"] < 500, (profile, req, page["status"])
                files[f"{profile}/{file_name(i, req)}"] = dump_json(page)
    return files


def main() -> None:
    root = write_corpus(NAME, extract())
    print(f"wrote {root}")


if __name__ == "__main__":
    main()
