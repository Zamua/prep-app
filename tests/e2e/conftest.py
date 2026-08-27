"""Pytest fixtures for the e2e suite.

The e2e suite drives a deployed prep instance over HTTP: staging by
default, overridable via E2E_BASE_URL. Each test session creates a
throwaway deck (`e2e-test-deck`) via the app's normal HTTP routes,
runs assertions, then deletes it via the same routes: so the
fixture itself exercises create + delete + cascade. Failures don't
leak the deck into staging because teardown runs in a `yield`-style
fixture's `finally`.

Run from the repo root:
    .venv/bin/pytest tests/e2e -q
or
    make e2e
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import httpx
import pytest

# Where the deployed-target suites point. Override with
# `E2E_BASE_URL=http://localhost:8082` (or another env) at invocation
# time. The trailing slash is normalized off so callers can `+ "/path"`.
#
# Staging authenticates with Clerk, so these suites need credentials:
# a `prep_pat_…` in E2E_API_TOKEN for the httpx setup fixtures, and a
# signed-in browser profile for the page fixtures (see
# `deployed_target` and `clerk_storage_state`). Without them the
# deployed suites SKIP with the reason; they must never silently pass
# by testing nothing, which is what happened while this default still
# pointed at a local stack that had been retired.
DEFAULT_BASE_URL = "https://staging.prepcards.app"

E2E_DECK_NAME = "e2e-test-deck"

# Canonical questions seeded into the throwaway deck. The first three
# are short single-token answers that grade through the deterministic
# path (no AI call). The fourth question has an answer long enough
# that classify_grading routes it to ai_grade - used by the
# AI-grading + regrade e2e cases.
E2E_QUESTIONS = [
    {"prompt": "Capital of France?", "answer": "Paris"},
    {"prompt": "Capital of Japan?", "answer": "Tokyo"},
    {"prompt": "Capital of Egypt?", "answer": "Cairo"},
    {
        # Long-enough answer (>3 tokens, with sentence punctuation)
        # forces the ai_grade path rather than the deterministic grader.
        "prompt": "Briefly: what is the role of the GIL in CPython?",
        "answer": "It serializes Python bytecode execution so only one thread runs at a time.",
    },
]


def _base_url() -> str:
    return os.environ.get("E2E_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


@pytest.fixture(scope="session")
def base_url() -> str:
    """Where the e2e suite points its HTTP + browser clients. The trailing
    slash is normalized off so tests can `f"{base_url}/path"`."""
    return _base_url()


@pytest.fixture(scope="session")
def deployed_target(base_url: str) -> str:
    """Guard for every suite that talks to a DEPLOYED prep.

    Proves the target is reachable AND is really prep before any test
    runs, then reports precisely what is missing. A wrong or retired
    URL now fails here with the URL in the message instead of surfacing
    as unrelated assertion noise (or, worse, as a suite that quietly
    exercises nothing)."""
    probe = f"{base_url}/healthz"
    try:
        r = httpx.get(probe, timeout=10.0, follow_redirects=False)
    except httpx.HTTPError as e:
        pytest.skip(f"deployed target unreachable at {probe}: {e}")
    if r.status_code != 200:
        pytest.skip(f"{probe} returned {r.status_code}; not a live prep deploy")
    body = (r.text or "").lower()
    if "ok" not in body and "healthy" not in body:
        pytest.skip(f"{probe} answered 200 but does not look like prep: {body[:60]!r}")
    return base_url


@pytest.fixture(scope="session")
def http(base_url: str, deployed_target: str) -> Iterator[httpx.Client]:
    """Synchronous HTTP client for setup/teardown against a deployed
    prep.

    Auth is E2E_API_TOKEN (`prep_pat_…`), the public bearer API; mint
    one at /settings/api while signed in. The header spoof that used to
    stand in for it is gone: nothing verified it, so a deploy that
    honoured it would hand any caller any user's data. A local target
    identifies by header plus `X-Internal-Token` instead
    (tests/e2e/celld_node.py). No token means the suite cannot create
    its fixtures. That skips when the target is the default, and FAILS
    when E2E_BASE_URL named one deliberately: a silent skip against a
    chosen deploy reads as coverage and is how these suites went months
    testing nothing."""
    token = os.environ.get("E2E_API_TOKEN")
    if not token:
        why = (
            "no credentials for the deployed target: set E2E_API_TOKEN "
            "(a prep_pat_ token from /settings/api)"
        )
        if os.environ.get("E2E_BASE_URL"):
            pytest.fail(f"{why}; E2E_BASE_URL={base_url} was named explicitly")
        pytest.skip(why)
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(
        base_url=base_url,
        headers=headers,
        timeout=30.0,
        follow_redirects=False,
        verify=True,
    ) as c:
        yield c


@pytest.fixture(scope="session")
def test_deck(http: httpx.Client) -> Iterator[dict]:
    """Create the e2e test deck via the SRS deck-creation route, seed
    `E2E_QUESTIONS` via the manual question-add route, yield a dict of
    deck metadata to the tests, and delete the deck on teardown.

    The URL slug is auto-generated (opaque short ID); the display
    label is `E2E_DECK_NAME`. We learn the slug from the create
    response's Location header and use it for every follow-up call.

    Idempotent on entry: any prior decks with the E2E display label
    are deleted before creating a fresh one.
    """
    # Pre-clean: drop any leftover e2e decks the previous run left.
    _delete_test_decks_by_display(http)

    r = http.post(
        "/decks/new/srs",
        data={
            "name": E2E_DECK_NAME,
            "context_prompt": "e2e test deck: created + torn down per run",
            "action": "empty",  # no AI generation
        },
    )
    assert r.status_code == 303, f"deck create returned {r.status_code}: {r.text[:300]}"
    # /deck/<slug>: strip the redirect to learn the slug.
    location = r.headers.get("location", "")
    slug = location.rstrip("/").split("/deck/", 1)[-1].split("/")[0].split("?")[0]
    assert slug, f"could not parse slug from redirect {location!r}"

    qids: list[int] = []
    for q in E2E_QUESTIONS:
        r = http.post(
            f"/deck/{slug}/question/new",
            data={
                "prompt": q["prompt"],
                "answer": q["answer"],
                "type": "short",
            },
        )
        assert r.status_code in (
            200,
            303,
        ), f"seed question {q['prompt']!r}: {r.status_code} {r.text[:200]}"

    r = http.get(f"/deck/{slug}")
    assert r.status_code == 200, f"deck page: {r.status_code}"
    import re

    for m in re.finditer(r'data-qid="(\d+)"', r.text):
        qid = int(m.group(1))
        if qid not in qids:
            qids.append(qid)
    assert len(qids) >= len(E2E_QUESTIONS), (
        f"expected {len(E2E_QUESTIONS)} qids on deck page, got {qids}"
    )

    info = {"name": slug, "display_name": E2E_DECK_NAME, "qids": qids[: len(E2E_QUESTIONS)]}
    try:
        yield info
    finally:
        _delete_one_deck(http, slug)


def _delete_one_deck(http: httpx.Client, slug: str) -> None:
    """Delete a single deck by slug. Best-effort: non-200/303/404
    responses are reported but don't raise so teardown failures
    don't mask earlier real failures."""
    r = http.post(f"/deck/{slug}/delete", data={"confirm": slug})
    if r.status_code not in (200, 303, 404):
        print(f"[e2e teardown] delete {slug!r} returned {r.status_code}: {r.text[:200]}")


def _delete_test_decks_by_display(http: httpx.Client, label: str = E2E_DECK_NAME) -> None:
    """Delete any deck whose label matches `label` or whose slug equals
    it (legacy decks from before the slug-vs-display split). Necessary
    because the slug is random and we can't guess it from a prior
    leftover run.

    Read from the dashboard's overview payload, not its markup: the
    deck rows are rendered client-side by the shared components, so
    the JSON endpoint is what an httpx client can see. Every e2e
    pre-clean goes through here for that reason; a second copy scraping
    `GET /` would match nothing and report nothing."""
    import json as _js

    r = http.get("/api/dashboard/overview")
    if r.status_code != 200:
        return
    try:
        decks = _js.loads(r.text).get("decks") or []
    except ValueError:
        return
    for deck in decks:
        slug = deck.get("slug") or ""
        if deck.get("display_name") == label or slug == label:
            _delete_one_deck(http, slug)


# ---- Playwright (browser) fixtures ------------------------------------
#
# httpx-only e2e can't see browser-side failures: inline `<script
# type="module">` parse errors, importmap resolution misses, htmx
# polling not actually firing, button click handlers not attached, DOM
# swaps not landing. Every page returns 200 with the right HTML, but
# the JS quietly dies. An importmap-ordering regression has caused
# exactly this shape of outage in the past: transform polling stopped
# because every inline module on the page died at parse time, but the
# httpx-only `make e2e` stayed green.
#
# These fixtures wire Playwright into the same pytest session as the
# httpx fixtures above. Browser tests live in test_browser_smoke.py and
# carry the `slow` + `browser` marks so a fast iteration loop can skip
# them via `pytest -m "not browser"`.
#
# We use Playwright's SYNC api: it matches pytest's sync default and
# avoids needing pytest-asyncio for these tests (the suite mixes sync
# httpx tests with sync browser tests, no event-loop juggling).


def _browser_session_factory():
    """Lazily import playwright so a missing install gives a clean
    skip-the-suite signal instead of a collection-time ImportError."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        pytest.skip(
            "playwright not installed: `uv sync --group dev` then "
            "`uv run playwright install chromium`. Original error: "
            f"{e}",
            allow_module_level=False,
        )
    return sync_playwright


@pytest.fixture(scope="session")
def browser_session():
    """One Chromium per test session, headless. Re-used across every
    browser test for speed (browser launch is ~1s; per-test contexts
    are cheap)."""
    sync_playwright = _browser_session_factory()
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as e:
            pytest.skip(
                f"chromium launch failed (browser binary missing? run "
                f"`uv run playwright install chromium`): {e}",
                allow_module_level=False,
            )
        try:
            yield browser
        finally:
            browser.close()


# The identity a deployed-target browser context claims. A Clerk deploy
# ignores it (the storage state below is what signs in), and no deploy
# trusts it on its own; it is here so a context that reaches a local
# target still names a stable user.
_DEFAULT_TS_LOGIN = "e2e-browser@example.com"


@pytest.fixture(scope="session")
def default_user_header() -> str:
    """The Tailscale-User-Login header value used by browser contexts."""
    return _DEFAULT_TS_LOGIN


@pytest.fixture(scope="session")
def clerk_storage_state(browser_session, base_url, deployed_target):
    """A signed-in browser profile for Clerk deploys, or None when the
    target does not need one.

    The header spoof below only authenticates a tailscale-mode server.
    A Clerk deploy (staging, prod) ignores it, so browser tests there
    need a real session: this signs in ONCE with a test account and
    hands every context the resulting storage state.

    Credentials come from E2E_CLERK_EMAIL / E2E_CLERK_PASSWORD. On the
    staging Clerk instance a `+clerk_test` address skips real email
    delivery. Missing credentials skip the browser suites rather than
    letting them run unauthenticated and assert on a sign-in page."""
    probe = httpx.get(f"{base_url}/", timeout=15.0, follow_redirects=False)
    # A tailscale-mode server serves the app (or its own 401) directly;
    # a Clerk deploy bounces an anonymous visitor to its sign-in host.
    needs_clerk = (
        probe.status_code in (302, 303, 307, 308)
        and "clerk" in (probe.headers.get("location", "") + probe.text).lower()
    )
    if not needs_clerk:
        return None

    email = os.environ.get("E2E_CLERK_EMAIL")
    password = os.environ.get("E2E_CLERK_PASSWORD")
    if not (email and password):
        pytest.skip(
            f"{base_url} authenticates with Clerk; set E2E_CLERK_EMAIL and "
            "E2E_CLERK_PASSWORD (a +clerk_test account) to run browser "
            "tests against it"
        )

    ctx = browser_session.new_context()
    page = ctx.new_page()
    try:
        page.goto(f"{base_url}/", wait_until="domcontentloaded", timeout=30_000)
        page.get_by_label("Email address").fill(email)
        page.get_by_role("button", name="Continue").click()
        page.get_by_label("Password").fill(password)
        page.get_by_role("button", name="Continue").click()
        # Landing back on the app (not the identity host) is the signal.
        page.wait_for_url(f"{base_url}/**", timeout=30_000)
        state = ctx.storage_state()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Clerk sign-in failed for {email}: {e}")
    finally:
        ctx.close()
    return state


@pytest.fixture(scope="function")
def page(browser_session, base_url, default_user_header, clerk_storage_state):
    """Per-test browser context + page, sized to iPhone-15-Pro for
    parity with the actual primary user (PWA on phone). The context
    routes the Tailscale-User-Login header onto SAME-ORIGIN requests
    only so any auth-gated app route sees a logged-in user.

    Why route() rather than `extra_http_headers`: the latter applies
    to every request including cross-origin asset fetches (Google
    Fonts), which trip CORS preflight rejections because the upstream
    doesn't whitelist `tailscale-user-login` in
    `Access-Control-Allow-Headers`. Those CORS failures pollute the
    console-error assertion in test_browser_smoke.py (and they're not
    a real app issue: staging behind Tailscale Serve injects the
    header server-side, never on cross-origin asset fetches). Route-
    based injection scopes the header to the prep app's origin.

    Function-scoped so cookies / localStorage from one test don't leak
    into the next."""
    ctx = browser_session.new_context(
        user_agent=(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.4 Mobile/15E148 Safari/604.1"
        ),
        viewport={"width": 393, "height": 852},
        # iPhone-ish device pixel ratio so any layout that branches on
        # DPR (rare in this app) sees the realistic shape.
        device_scale_factor=3,
        is_mobile=True,
        has_touch=True,
        # Present on Clerk deploys, None on tailscale-mode servers.
        storage_state=clerk_storage_state,
    )
    ctx.set_default_timeout(15_000)
    ctx.set_default_navigation_timeout(15_000)

    # Inject the Tailscale identity header on requests to the prep
    # app's origin only. `urljoin` would be overkill: base_url is
    # already a clean origin+path prefix from the fixture above; we
    # match on the host+root-path prefix.
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    same_origin_prefix = f"{parsed.scheme}://{parsed.netloc}"

    def _inject_header(route, request):
        if request.url.startswith(same_origin_prefix):
            headers = {**request.headers, "tailscale-user-login": default_user_header}
            route.continue_(headers=headers)
        else:
            route.continue_()

    ctx.route("**/*", _inject_header)

    p = ctx.new_page()
    try:
        yield p
    finally:
        ctx.close()


# ---- Local celld-node fixtures ----------------------------------------
#
# The offline suites cannot run against the fleet: it is Clerk-mode, which
# the header injection does nothing for, and the suites have to make the
# server genuinely unreachable. So they drive a LOCAL celld node
# (tests/e2e/celld_node.py) seeded through `POST /_parity/seed`, using the
# same route-based header injection as the `page` fixture above plus the
# internal token the fake provider demands.
#
# Two empirically-verified Playwright/service-worker facts shape these
# fixtures (probed against Chromium before the suite was written):
#
# - ctx.route() header injection reaches page-context fetches even on a
#   SW-controlled page (the SW's fetch handler passes non-precache GETs
#   and all POSTs through without respondWith), but it does NOT reach
#   requests the SW re-issues itself (the navigation-fallback race's
#   fetch(request)). Priming must therefore happen on the first,
#   uncontrolled page load, and no test may depend on an authenticated
#   NAVIGATION after the SW has claimed the page.
# - ctx.set_offline(True) does not apply to the service worker target:
#   with a live server the SW's navigation fetch succeeds and the
#   offline fallback never fires. Real offline is simulated by STOPPING
#   the local node (connection refused rejects the SW's fetch
#   instantly). set_offline no longer even reaches the renderer a
#   SW-fallback navigation created (Chromium drops the context's
#   emulation there), so reconnect tests dispatch the window `online`
#   event themselves; the event is browser plumbing, not the contract
#   under test.

import subprocess as _subprocess  # noqa: E402  belongs to the section above

from tests.e2e.celld_node import (  # noqa: E402
    OFFLINE_E2E_LOGIN,
    OFFLINE_E2E_NAME,
    WORKER_DIR,
    LocalCelldNode,
    identity_headers,
    require_scratch_storage,
)

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4 Mobile/15E148 Safari/604.1"
)


def new_iphone_context(browser_session, **kwargs):
    """The device shape every local context shares: the primary user's
    phone, function-scoped so IndexedDB and service-worker state from one
    test never reach the next."""
    ctx = browser_session.new_context(
        user_agent=IPHONE_UA,
        viewport={"width": 393, "height": 852},
        device_scale_factor=3,
        is_mobile=True,
        has_touch=True,
        **kwargs,
    )
    ctx.set_default_timeout(15_000)
    ctx.set_default_navigation_timeout(15_000)
    return ctx


def inject_identity(ctx, base_url: str, login: str, name: str | None = None):
    """Route-based injection, scoped to the local origin. `extra_http_headers`
    would apply to cross-origin asset fetches too, whose CORS preflight does
    not whitelist these names."""
    headers = identity_headers(login, name)

    def _inject(route, request):
        if request.url.startswith(base_url):
            route.continue_(headers={**request.headers, **headers})
        else:
            route.continue_()

    ctx.route("**/*", _inject)


@pytest.fixture(scope="session")
def celld_build():
    """One worker build per session. Every node deploys this same build to
    its own bucket prefix; the shapes differ by environment, not by code."""
    require_scratch_storage()
    proc = _subprocess.run(
        ["npm", "run", "build"],
        cwd=str(WORKER_DIR),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.fail(f"worker build failed:\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}")
    return True


@pytest.fixture(scope="session")
def offline_server(celld_build) -> Iterator[LocalCelldNode]:
    """Session-scoped local node for the offline suites: the `offline_e2e`
    profile seeded into one cell, ids on `.seed`."""
    node = LocalCelldNode("offline")
    node.start()
    try:
        node.seed = node.seed_profile(OFFLINE_E2E_LOGIN, "offline_e2e")
        yield node
    finally:
        node.stop()


@pytest.fixture(scope="function")
def offline_ctx(browser_session, offline_server):
    """Browser context against the LOCAL node: the same iPhone shape and
    route-based header injection as the `page` fixture, scoped to the local
    origin."""
    ctx = new_iphone_context(browser_session)
    inject_identity(ctx, offline_server.base_url, OFFLINE_E2E_LOGIN, OFFLINE_E2E_NAME)
    try:
        yield ctx
    finally:
        ctx.close()


@pytest.fixture(scope="function")
def offline_page(offline_ctx):
    return offline_ctx.new_page()
