"""Pytest fixtures for the e2e suite.

The e2e suite drives a deployed prep instance over HTTP — staging by
default, overridable via E2E_BASE_URL. Each test session creates a
throwaway deck (`e2e-test-deck`) via the app's normal HTTP routes,
runs assertions, then deletes it via the same routes — so the
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

# Default points at staging behind Tailscale serve. Override locally with
# `E2E_BASE_URL=http://localhost:8082` (or another env) at invocation
# time. The trailing slash is normalized off so callers can `+ "/path"`.
DEFAULT_BASE_URL = "https://macmini.trout-chimera.ts.net/prep-staging"

E2E_DECK_NAME = "e2e-test-deck"

# Canonical questions seeded into the throwaway deck. The first three
# are short single-token answers that grade through the deterministic
# path (no claude). The "claude" question has an answer long enough
# that classify_grading routes it to claude_grade — used by the
# claude-grading + regrade e2e cases.
E2E_QUESTIONS = [
    {"prompt": "Capital of France?", "answer": "Paris"},
    {"prompt": "Capital of Japan?", "answer": "Tokyo"},
    {"prompt": "Capital of Egypt?", "answer": "Cairo"},
    {
        # Long-enough answer (>3 tokens, with sentence punctuation)
        # forces claude_grade per prep.trivia.service.classify_grading.
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
def http(base_url: str) -> Iterator[httpx.Client]:
    """Synchronous HTTP client for setup/teardown. Tailscale serve on
    the same machine auto-injects identity headers, so no auth setup
    needed when running on the mac mini host. Outside that environment
    set E2E_TAILSCALE_LOGIN to spoof for local fastapi dev."""
    headers = {}
    spoof = os.environ.get("E2E_TAILSCALE_LOGIN")
    if spoof:
        headers["Tailscale-User-Login"] = spoof
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
    """Create the e2e-test-deck via the SRS deck-creation route, seed
    `E2E_QUESTIONS` via the manual question-add route, yield a dict of
    deck metadata to the tests, and delete the deck on teardown.

    Idempotent on entry: if a prior run left the deck behind (e.g.
    interrupted teardown), we delete it first.
    """
    # Pre-clean: if a prior failed run left the deck, drop it first
    # so create succeeds. Delete is idempotent.
    _delete_test_deck(http)

    # Create — SRS path with no AI agent involvement, fastest path.
    r = http.post(
        "/decks/new/srs",
        data={
            "name": E2E_DECK_NAME,
            "context_prompt": "e2e test deck — created + torn down per run",
            "action": "empty",  # no claude generation
        },
    )
    assert r.status_code in (200, 303), f"deck create returned {r.status_code}: {r.text[:300]}"

    # Seed questions via the manual add route.
    qids: list[int] = []
    for q in E2E_QUESTIONS:
        r = http.post(
            f"/deck/{E2E_DECK_NAME}/question/new",
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

    # Pull the question ids back. The deck page exposes them via
    # data-qid attributes on each .qcard.
    r = http.get(f"/deck/{E2E_DECK_NAME}")
    assert r.status_code == 200, f"deck page: {r.status_code}"
    import re

    for m in re.finditer(r'data-qid="(\d+)"', r.text):
        qid = int(m.group(1))
        if qid not in qids:
            qids.append(qid)
    assert len(qids) >= len(
        E2E_QUESTIONS
    ), f"expected {len(E2E_QUESTIONS)} qids on deck page, got {qids}"

    info = {"name": E2E_DECK_NAME, "qids": qids[: len(E2E_QUESTIONS)]}
    try:
        yield info
    finally:
        _delete_test_deck(http)


def _delete_test_deck(http: httpx.Client) -> None:
    """Best-effort delete. The app requires the deck name in the
    `confirm` field as a typo guard; we always pass it. 303 = success,
    404 = already gone (also fine), other = the test reports it."""
    r = http.post(
        f"/deck/{E2E_DECK_NAME}/delete",
        data={"confirm": E2E_DECK_NAME},
    )
    if r.status_code not in (200, 303, 404):
        # Surface non-fatally — we don't want a teardown failure to mask
        # a real test failure earlier in the session.
        print(f"[e2e teardown] deck delete returned {r.status_code}: {r.text[:200]}")


# ---- Playwright (browser) fixtures ------------------------------------
#
# httpx-only e2e can't see browser-side failures: inline `<script
# type="module">` parse errors, importmap resolution misses, htmx
# polling not actually firing, button click handlers not attached, DOM
# swaps not landing. Every page returns 200 with the right HTML, but
# the JS quietly dies. The 2026-05-10 outage was exactly this shape —
# transform polling stopped because an importmap-ordering regression
# nuked every inline module on the page, but `make e2e` was green.
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
            "playwright not installed — `uv sync --group dev` then "
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


# Default Tailscale-User-Login spoof for the browser context. The
# httpx fixtures above use E2E_TAILSCALE_LOGIN when running off-host;
# on-host, the Tailscale Serve proxy injects the real header so this
# header is silently overwritten upstream — fine. For dev contributors
# pointing at a local fastapi instance, set E2E_TAILSCALE_LOGIN to a
# stable identity so created decks attach to a known user.
_DEFAULT_TS_LOGIN = "e2e-browser@example.com"


@pytest.fixture(scope="session")
def default_user_header() -> str:
    """The Tailscale-User-Login header value used by browser contexts.
    Honors E2E_TAILSCALE_LOGIN so the same env var the httpx fixtures
    use also applies here. The default identity isn't load-bearing on
    staging (Tailscale Serve overwrites it) but matters off-host."""
    return os.environ.get("E2E_TAILSCALE_LOGIN", _DEFAULT_TS_LOGIN)


@pytest.fixture(scope="function")
def page(browser_session, base_url, default_user_header):
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
    a real app issue — staging behind Tailscale Serve injects the
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
    )
    ctx.set_default_timeout(15_000)
    ctx.set_default_navigation_timeout(15_000)

    # Inject the Tailscale identity header on requests to the prep
    # app's origin only. `urljoin` would be overkill — base_url is
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
