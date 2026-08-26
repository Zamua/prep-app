"""The pages corpus replayed against a running TypeScript server
(docs/PHASE-3.md F.4).

`worker/tests/pages/*.test.ts` compares the context a use case builds
against the recorded one, in process. This one issues the same requests
over HTTP and compares the DOM the server actually renders against the
golden the recorded context produces through the reference Jinja env, so
the context is pinned through the markup and the runtime, not only
through an object.

    PARITY_BASE_URL=http://127.0.0.1:8791 \
    PARITY_INTERNAL_TOKEN=parity-internal-token \
    .venv/bin/pytest tests/parity/test_ts_pages.py -q

Each profile replays in order: a request that sets a flag is what the
requests recorded under that flag depend on.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

from tests.parity.dom_diff import dom_diff
from tests.parity.harness.constants import (
    PARITY_USER,
    PARITY_USER_NAME,
    internal_token,
)
from tests.parity.harness.server import BASE_URL_ENV, seed
from tests.parity.oracles import REPO_ROOT, pin_clock, read_corpus
from tests.parity.oracles.contexts import fake_request
from tests.parity.oracles.pages import PROFILES, VOLATILE_MARK, file_name
from tests.parity.oracles.render_templates import _env

CORPUS = REPO_ROOT / "tests" / "fixtures" / "parity" / "pages"
# The host the corpus was recorded at: `app_base` is built from it, and a
# target reached at 127.0.0.1 would render a different one into every page.
PARITY_HOST = "parity.example.test"

# Decision 7.4: the deploy-wide subscription is retired, so its row is
# offered only while a stored credential still names it. The recording
# predates that and lists it as a fourth provider.
RETIRED_PROVIDER = "claude-subscription"

# The badge reads `display_label`, `display_status` and the two state flags
# off each row. Those are derived from the columns at render time, and the
# recorded context holds the columns, so the reference renderer has nothing
# to read: only the status and the content type are comparable here. The
# markup itself is covered by the `html` corpus, whose contexts carry the
# derived fields.
DERIVED_ONLY = frozenset({"partials/workflow_badge.html"})


def requests_for(profile: str) -> list[tuple[str, object]]:
    """The recorded requests of a profile, in order, each with the corpus
    file it produced. Driven from the extractor's own list: the form body a
    request posts is what the pages recorded after it depend on, and the
    corpus file does not carry it."""
    seed_profile, build = PROFILES[profile]
    ids = json.loads(read_corpus("pages")[f"{profile}/seed.json"]) if seed_profile else {}
    return [(file_name(i, req), req) for i, req in enumerate(build(ids), start=1)]


def cases() -> list[tuple[str, str]]:
    return [(profile, name) for profile in PROFILES for name, _ in requests_for(profile)]


def base_url() -> str:
    url = os.environ.get(BASE_URL_ENV)
    if not url:
        pytest.skip(f"set {BASE_URL_ENV} to a running TypeScript parity server")
    return url.rstrip("/")


def headers_for(profile: str) -> dict[str, str]:
    """The identity the recording browsed as. The `anonymous` profile is a
    visitor, which is no identity at all."""
    common = {"X-Forwarded-Proto": "https", "Host": PARITY_HOST}
    if PROFILES[profile][0] is None:
        return common
    return {
        **common,
        "Tailscale-User-Login": PARITY_USER,
        "Tailscale-User-Name": PARITY_USER_NAME,
        "X-Internal-Token": internal_token(None),
    }


def golden(page: dict, vapid_key: str) -> str:
    """The recorded context rendered through the reference env. The deck
    display map is a callable in a template, and the VAPID key is the
    target's own: the corpus pins it as unreproducible."""
    context = dict(page["context"])
    sections = context.get("byok_sections")
    if sections:
        context["byok_sections"] = [
            s for s in sections if s["provider"] != RETIRED_PROVIDER or s.get("metadata")
        ]
    display = context.get("deck_display") or {}
    context["deck_display"] = lambda slug: display.get(slug, slug) if slug else ""
    if context.get("vapid_key") == VOLATILE_MARK:
        context["vapid_key"] = vapid_key
    context.setdefault("request", fake_request(page["path"].split("?")[0]))
    # The recorded timestamps are relative to the parity instant; on the wall
    # clock every one of them reads as months ago.
    with pin_clock():
        return _env().get_template(page["template"]).render(context)


@pytest.fixture(scope="module")
def served() -> dict[str, dict]:
    """Every recorded request replayed in order, per profile, keyed by the
    corpus file name."""
    url = base_url()
    token = internal_token(None)
    out: dict[str, dict] = {}
    with httpx.Client(base_url=url, follow_redirects=False, timeout=60.0) as client:
        vapid = client.get("/notify/vapid-public-key").json()["key"]
        for profile, (seed_profile, _) in PROFILES.items():
            if seed_profile:
                seed(url, PARITY_USER, seed_profile, token=token)
            for name, req in requests_for(profile):
                page = json.loads((CORPUS / profile / name).read_text(encoding="utf-8"))
                response = client.request(
                    req.method,
                    req.path,
                    headers=headers_for(profile),
                    data=req.data,
                )
                out[f"{profile}/{name}"] = {
                    "page": page,
                    "status": response.status_code,
                    "content_type": response.headers.get("content-type", ""),
                    "location": response.headers.get("location"),
                    "text": response.text,
                    "vapid": vapid,
                }
    return out


@pytest.mark.parametrize(("profile", "name"), cases(), ids=lambda v: v)
def test_page_matches_the_corpus(served: dict[str, dict], profile: str, name: str):
    actual = served[f"{profile}/{name}"]
    page = actual["page"]
    where = f"{page['method']} {page['path']}"

    assert actual["status"] == page["status"], where
    expected_type = page["headers"].get("content-type", "")
    if expected_type:
        assert actual["content_type"] == expected_type, where
    if "location" in page["headers"]:
        assert actual["location"] == page["headers"]["location"], where

    if "template" not in page:
        # A redirect or an empty body: the status and the location are the
        # whole of it, and the corpus records no render.
        assert actual["text"] == page.get("body", ""), where
        return
    if page["template"] in DERIVED_ONLY:
        return

    diffs = dom_diff(golden(page, actual["vapid"]), actual["text"])
    assert not diffs, (
        f"{where}\n"
        + "\n".join(str(d) for d in diffs[:20])
        + (f"\n... {len(diffs) - 20} more" if len(diffs) > 20 else "")
    )


def test_every_recorded_request_is_replayed(served: dict[str, dict]):
    assert len(served) == len(cases())
    assert len(served) == 31
