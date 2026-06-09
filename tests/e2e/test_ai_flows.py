"""End-to-end coverage for the AI-driven workflows: transform, plan,
trivia generation.

The bug class these guard against: htmx-polling regressions where
button-press routes hang on `await handle.result()`, and a
fragment-only `make e2e` doesn't catch it because no test exercises
these flows end-to-end. Each test below drives the workflow from the
form-POST through to a terminal status, then asserts on the final
fragment HTML (state markers, presence/absence of the htmx polling
trigger, presence of the user-visible action UI).

Structure mirrors `test_smoke.py` (httpx-only, against a deployed
prep instance — staging by default, override via `E2E_BASE_URL`).
Each test makes real claude calls so they're slow (30-90s typically).
Marked `@pytest.mark.slow` so a fast iteration loop can skip them via
`pytest -m "not slow"`. `make e2e` runs them by default — they are
the gate the promote flow leans on.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from urllib.parse import urlparse

import httpx
import pytest

# Polling configuration. The htmx fragments themselves poll every 1.5–2s
# in the browser; we use the same cadence so the test exercises the same
# server-side load shape. The max-wait ceiling is generous because
# claude latency varies wildly and we'd rather see a real failure than a
# flake.
POLL_INTERVAL_S = 2.0
# General-purpose ceiling for transform / plan flows. Transform on the
# small e2e deck typically returns in 30-60s; plan generation in 5-15s.
MAX_WAIT_S = 240.0  # 4 min
# Trivia batch generation is a single claude call that returns 25 cards
# with prompts + answers + 2-4-sentence explanations — large output,
# slower than a transform plan. Worker's HTTPAgent client times out at
# 5min per call, and the activity has MaximumAttempts=2, so the
# worst-case is one timeout + one successful retry ≈ 5-9 min.
MAX_WAIT_TRIVIA_S = 600.0  # 10 min


# ---- helpers -----------------------------------------------------------


def _wid_from_redirect(response: httpx.Response, prefix: str) -> str:
    """Pull the workflow id out of a 303 Location header.

    The app's `responses.redirect` prepends `root_path`, so a Location
    looks like `/prep-staging/transform/transform-deck-7-abcd1234`.
    Strip the leading prefix path component to get the workflow id."""
    assert response.status_code in (
        200,
        303,
    ), f"expected redirect, got {response.status_code}: {response.text[:300]}"
    loc = response.headers.get("location")
    assert loc, f"no Location header on redirect (status={response.status_code})"
    # Location may be absolute or path-only; normalize to path.
    path = urlparse(loc).path if loc.startswith("http") else loc
    return path.rsplit("/", 1)[-1]


def _poll_until(
    http: httpx.Client,
    url: str,
    predicate,
    *,
    timeout_s: float = MAX_WAIT_S,
    interval_s: float = POLL_INTERVAL_S,
) -> tuple[httpx.Response, str]:
    """Poll `url` until `predicate(response)` is truthy or timeout. Returns
    the final (response, status_extracted_from_data_status_attr) tuple.

    Status is extracted from the `data-status="..."` attribute the
    progress fragments embed on their `.transform-status` /
    `.trivia-gen-status` element — this is the same hook the server
    uses to drive the htmx polling lifecycle so it's a stable source
    of truth across the three workflow types."""
    deadline = time.monotonic() + timeout_s
    last_status = ""
    last_response: httpx.Response | None = None
    while time.monotonic() < deadline:
        r = http.get(url, timeout=15.0)
        last_response = r
        assert r.status_code == 200, f"poll {url} → {r.status_code}: {r.text[:300]}"
        m = re.search(r'data-status="([^"]*)"', r.text)
        last_status = m.group(1) if m else ""
        if predicate(r, last_status):
            return r, last_status
        time.sleep(interval_s)
    raise AssertionError(
        f"timed out after {timeout_s}s polling {url} — last status={last_status!r}, "
        f"last body head: {(last_response.text[:400] if last_response else '<none>')!r}"
    )


def _has_polling_trigger(html: str) -> bool:
    """True if the fragment carries `hx-trigger="every <X>s"` —
    i.e., htmx will keep polling. Server stops polling by omitting
    the attribute on terminal-state fragments; this helper proves
    the regression is gone."""
    return bool(re.search(r'hx-trigger="every\s+\d', html))


def _count_qids_in_deck(http: httpx.Client, deck_name: str) -> int:
    """Count `data-qid="..."` occurrences on the deck page — same
    primitive the conftest fixture uses to harvest qids. The deck page
    renders one entry per question; transform-add-cards therefore
    increments this count."""
    r = http.get(f"/deck/{deck_name}")
    assert r.status_code == 200, f"deck page {deck_name} → {r.status_code}"
    return len(re.findall(r'data-qid="\d+"', r.text))


# ---- fixtures specific to this file -----------------------------------


@pytest.fixture(scope="function")
def e2e_trivia_deck(http: httpx.Client) -> Iterator[dict]:
    """Throwaway trivia deck for trivia-flow e2e tests.

    Function-scoped so the test body owns the deck end-to-end (vs.
    sharing across tests, which would interfere because the
    /decks/new/trivia endpoint kicks off a long-running workflow we
    want to assert on per-test). Uses a hyphenated name distinct from
    `e2e-test-deck` (the SRS one) so a stranded deck from one fixture
    doesn't collide with the other.

    The trivia create endpoint synchronously creates the deck row AND
    starts the TriviaGenerateWorkflow; we capture the workflow id from
    the redirect so the test can drive it. Teardown cancels via the
    delete route — the deck-delete cascades through cards and review
    state."""
    display_name = "e2e-trivia-deck"
    # Pre-clean: scrape index for any prior leftover with this display
    # name (slug is opaque-random so we can't guess it). Reuses the SRS
    # fixture's helper.
    from tests.e2e.conftest import E2E_DECK_NAME as _SRS_LABEL  # noqa: F401
    from tests.e2e.conftest import _delete_one_deck

    r = http.get("/")
    if r.status_code == 200:
        pattern = re.compile(
            r'<a\s+href="[^"]*?/deck/([^"/]+)"[^>]*class="deck-link"[\s\S]*?'
            r'<span\s+class="deck-name">\s*([^<\n]+)',
        )
        for m in pattern.finditer(r.text):
            if m.group(2).strip() == display_name:
                _delete_one_deck(http, m.group(1))

    r = http.post(
        "/decks/new/trivia",
        data={
            "name": display_name,
            "topic": "world capitals: short single-token answers, no trivia about people",
            "notification_interval_minutes": "30",
        },
    )
    assert r.status_code == 303, f"trivia create returned {r.status_code}: {r.text[:300]}"
    loc = r.headers.get("location", "")
    # Location is /<root>/trivia/gen/<wid>; strip and extract the wid.
    wid_match = re.search(r"/trivia/gen/([^/?#]+)", loc)
    assert wid_match, f"no /trivia/gen/<wid> in redirect: {loc!r}"
    wid = wid_match.group(1)
    # wid shape is `trivia-<slug>-<random>`; the slug is what we need
    # for /deck/<slug> lookups.
    slug_match = re.match(r"^trivia-([a-z0-9]+)-[a-z0-9]+$", wid)
    assert slug_match, f"unexpected trivia wid shape: {wid!r}"
    slug = slug_match.group(1)
    info = {"name": slug, "display_name": display_name, "wid": wid}
    try:
        yield info
    finally:
        _delete_one_deck(http, slug)


# ---- transform flow ----------------------------------------------------


@pytest.mark.slow
def test_transform_flow_drives_to_awaiting_apply(http: httpx.Client, test_deck: dict):
    """Deck-scope transform: kick off → poll fragment → arrive at
    `awaiting_apply` with the accept/reject UI rendered + polling
    stopped. Then signal reject and assert the workflow lands on a
    terminal cancelled state with no further polling.

    This is the canonical "drive the htmx-polling lifecycle to
    terminal" e2e: covers the route-template-temporal-claude
    integration end-to-end."""
    name = test_deck["name"]
    # Fire the transform — small, scoped prompt so claude returns a
    # plan quickly. The intent is a no-op edit (rephrase prompts), not
    # to add or delete cards, so the test stays predictable on the
    # post-apply assertion in the next test.
    r = http.post(
        f"/deck/{name}/transform",
        data={
            "prompt": "rephrase each prompt to be slightly more concise; keep the same answer for each"
        },
    )
    wid = _wid_from_redirect(r, prefix="")
    assert wid.startswith("transform-deck-"), f"unexpected wid shape: {wid}"

    fragment_url = f"/transform/{wid}/fragment"

    # Poll until awaiting_apply (terminal-from-polling-POV).
    r, status = _poll_until(
        http,
        fragment_url,
        lambda resp, st: st == "awaiting_apply",
    )
    assert status == "awaiting_apply", status
    # Both action buttons must be present.
    assert f"/transform/{wid}/apply" in r.text, "apply form action missing"
    assert f"/transform/{wid}/reject" in r.text, "reject form action missing"
    # Polling must have stopped: server controls the loop, terminal
    # fragments omit hx-trigger so htmx ceases requests.
    assert not _has_polling_trigger(
        r.text
    ), f"awaiting_apply fragment still has hx-trigger; htmx will busy-poll. Body head: {r.text[:400]}"

    # Signal reject. The route returns the freshly-rendered fragment.
    r = http.post(f"/transform/{wid}/reject")
    assert r.status_code == 200, f"reject route returned {r.status_code}: {r.text[:300]}"
    # Initial post-reject fragment may show transient `rejecting`. Poll
    # until the workflow exits to a true terminal (rejected/gone/done).
    r, status = _poll_until(
        http,
        fragment_url,
        lambda resp, st: st in ("rejected", "gone", "done"),
    )
    assert status in (
        "rejected",
        "gone",
        "done",
    ), f"reject didn't drive to terminal: status={status!r}"
    # Cancelled-state copy on the headline.
    assert "Cancelled." in r.text or "Done." in r.text, "post-reject headline missing"
    # And no more polling.
    assert not _has_polling_trigger(
        r.text
    ), "post-reject terminal fragment still polls — htmx-trigger leak regression"


@pytest.mark.slow
def test_transform_apply_full_round_trip(http: httpx.Client, test_deck: dict):
    """Same setup as the reject test, but signal Apply on the
    awaiting-apply state and poll to `done`. Asserts the deck's
    question count reflects the applied plan (claude was asked to
    *add* a card — even one delta proves the apply path executed
    end-to-end through the InsertCard activity)."""
    name = test_deck["name"]
    initial_count = _count_qids_in_deck(http, name)
    # Add one explicit card. Asking for a single concrete addition is
    # more reliable than open-ended "rephrase" prompts because claude
    # will materialize a deterministic shape ("Capital of X?"), which
    # the test then verifies via the question-count delta.
    r = http.post(
        f"/deck/{name}/transform",
        data={
            "prompt": (
                "add exactly one new short-answer card asking 'Capital of Australia?' "
                "with the answer 'Canberra'. Do not modify or delete any existing cards."
            ),
        },
    )
    wid = _wid_from_redirect(r, prefix="")
    fragment_url = f"/transform/{wid}/fragment"

    # Wait for awaiting_apply.
    _, status = _poll_until(http, fragment_url, lambda resp, st: st == "awaiting_apply")
    assert status == "awaiting_apply", status

    # Apply.
    r = http.post(f"/transform/{wid}/apply")
    assert r.status_code == 200, f"apply route returned {r.status_code}: {r.text[:300]}"

    # Poll until done. Status passes through `applying` (transient) →
    # `done`. `gone` is acceptable too (workflow may have exited
    # before the next query lands).
    _, status = _poll_until(
        http,
        fragment_url,
        lambda resp, st: st in ("done", "gone"),
    )
    assert status in ("done", "gone"), f"apply didn't reach terminal: {status!r}"

    # Question count should have grown (the transform asked for one
    # addition; we don't lock to exactly +1 because claude may
    # legitimately split or merge under that prompt — but the
    # direction has to be up).
    final_count = _count_qids_in_deck(http, name)
    assert final_count > initial_count, (
        f"transform applied but no new cards visible on deck page "
        f"(initial={initial_count}, final={final_count})"
    )


# ---- plan flow ---------------------------------------------------------


@pytest.mark.slow
def test_plan_flow_drives_to_terminal(http: httpx.Client):
    """Plan-first SRS deck creation: POST /decks/new/srs with
    action=plan → redirected to /plan/<wid> → poll fragment until
    `awaiting_feedback` → reject → poll until `rejected`/`gone`.

    Uses its own throwaway deck (not the shared fixture) because the
    plan workflow is tied 1:1 to deck creation. Cleans up in the
    `finally` regardless of test outcome."""
    deck_name = "e2e-plan-deck"
    # Pre-clean idempotently.
    http.post(f"/deck/{deck_name}/delete", data={"confirm": deck_name})
    try:
        r = http.post(
            "/decks/new/srs",
            data={
                "name": deck_name,
                "context_prompt": (
                    "introductory world geography — three short cards on european capitals"
                ),
                "action": "plan",
            },
        )
        wid = _wid_from_redirect(r, prefix="")
        assert wid.startswith("plan-"), f"unexpected plan wid shape: {wid}"
        fragment_url = f"/plan/{wid}/fragment"

        # Poll until the plan is awaiting_feedback (terminal from the
        # polling-loop perspective — user must accept/reject).
        r, status = _poll_until(http, fragment_url, lambda resp, st: st == "awaiting_feedback")
        assert status == "awaiting_feedback", status
        # Plan content rendered: at least one plan-item shows up.
        assert (
            'class="plan-item"' in r.text or "plan-item-num" in r.text
        ), "no plan items rendered on awaiting_feedback fragment"
        # And polling has stopped server-side.
        assert not _has_polling_trigger(
            r.text
        ), "awaiting_feedback fragment still polls — htmx-trigger leak"

        # Reject the plan. Server returns the fresh fragment.
        r = http.post(f"/plan/{wid}/reject")
        assert r.status_code == 200, f"plan reject returned {r.status_code}: {r.text[:300]}"
        # Poll until the workflow lands at a real terminal.
        _, status = _poll_until(
            http,
            fragment_url,
            lambda resp, st: st in ("rejected", "gone", "failed", "done"),
        )
        assert status in (
            "rejected",
            "gone",
            "failed",
            "done",
        ), f"plan reject didn't reach terminal: {status!r}"
    finally:
        http.post(f"/deck/{deck_name}/delete", data={"confirm": deck_name})


# ---- trivia generation flow -------------------------------------------


@pytest.mark.slow
def test_trivia_generation_flow_drives_to_done(http: httpx.Client, e2e_trivia_deck: dict):
    """Trivia deck creation kicks off a TriviaGenerateWorkflow
    synchronously. Poll the generating fragment until `done`, then
    assert the deck page now lists generated cards.

    The fixture already created the deck + captured the wid from the
    redirect; the test drives the polling + asserts the post-state."""
    name = e2e_trivia_deck["name"]
    wid = e2e_trivia_deck["wid"]
    fragment_url = f"/trivia/gen/{wid}/fragment"

    _, status = _poll_until(
        http,
        fragment_url,
        lambda resp, st: st in ("done", "failed"),
        # 25 cards × (prompt + answer + 2-4-sentence explanation) is a
        # ~3-6KB JSON output; claude can take 1-4 minutes for a topic
        # with much surrounding context. Worst case the agent client
        # times out at 5min and the activity retries — total can hit
        # ~9 min on a slow day. Use the trivia-specific ceiling.
        timeout_s=MAX_WAIT_TRIVIA_S,
    )
    assert status == "done", f"trivia generation didn't complete: {status!r}"

    # Deck page should now show cards. Trivia decks render the same
    # `data-qid` attrs as SRS decks (deck.html template is shared).
    final_count = _count_qids_in_deck(http, name)
    assert final_count > 0, f"trivia generated `done` but deck has 0 cards: {final_count}"


# ---- regression guard: polling routes must not block ------------------


def test_no_blocking_handle_result_in_polling_routes(http: httpx.Client, test_deck: dict):
    """Each fragment-poll route MUST return promptly even when the
    workflow id is bogus / not-found / not-owned. If a future refactor
    re-introduces `await handle.result()` in the route handler, this
    test will hang for the temporal long-poll timeout instead of
    returning <500ms.

    Not marked slow — it's the cheapest test in this file (3 HTTP
    roundtrips, all expected to error fast). Lives here rather than in
    test_smoke.py because the assertion is specifically about the AI
    workflow polling endpoints we just added e2e for."""
    # 1. Transform fragment with a malformed wid (parses-but-not-found).
    #    Picks shape `transform-deck-<bogus-id>-<rand>` so the
    #    _parse_transform_wid succeeds and we hit the deck-not-found
    #    404 path (which is the same code path real users would hit on
    #    a stale URL after a deck-delete).
    bogus_wid = "transform-deck-99999999-bogus12345"
    t0 = time.monotonic()
    r = http.get(f"/transform/{bogus_wid}/fragment", timeout=5.0)
    elapsed = time.monotonic() - t0
    assert r.status_code in (404, 400), f"unexpected status {r.status_code}: {r.text[:200]}"
    assert elapsed < 0.5, f"/transform/<wid>/fragment hung {elapsed:.2f}s on bogus wid"

    # 2. Plan fragment, same shape — `plan-<deckname>-<rand>` against a
    #    deck name that doesn't exist for this user.
    bogus_plan_wid = "plan-no-such-deck-bogus12345"
    t0 = time.monotonic()
    r = http.get(f"/plan/{bogus_plan_wid}/fragment", timeout=5.0)
    elapsed = time.monotonic() - t0
    assert r.status_code in (404, 400), f"unexpected status {r.status_code}: {r.text[:200]}"
    assert elapsed < 0.5, f"/plan/<wid>/fragment hung {elapsed:.2f}s on bogus wid"

    # 3. Trivia-gen fragment.
    bogus_trivia_wid = "trivia-no-such-deck-bogus12345"
    t0 = time.monotonic()
    r = http.get(f"/trivia/gen/{bogus_trivia_wid}/fragment", timeout=5.0)
    elapsed = time.monotonic() - t0
    assert r.status_code in (404, 400), f"unexpected status {r.status_code}: {r.text[:200]}"
    assert elapsed < 0.5, f"/trivia/gen/<wid>/fragment hung {elapsed:.2f}s on bogus wid"
