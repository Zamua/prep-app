"""End-to-end instant-start landing
(docs/ANONYMOUS-ACCOUNTS.md sections 3 and 4, M2 milestone).

A successful generation is stored SERVER side under a freshly minted
anonymous account, and the endpoint answers with the URL to land on.
So the loop this file pins ends in a normal signed-in-shaped deck
page, not a device-local one, and the browser's only new state is the
`prep_anon` cookie.

Two ways to reach the endpoint, both with ZERO upstream spend:

- `live_ctx` lets the REAL endpoint run. A stub OpenAI-compatible
  inference server stands in for the deploy's free tier, so the deck
  is genuinely generated, stored, and served back. This is the only
  shape that can prove the redirect target exists.
- `instant_ctx` intercepts POST /api/instant/generate and serves
  canned responses, for the error branches where no deck is written.

The server is a LOCAL celld node started in the PUBLIC deploy shape
(clerk plus free-tier env): the instant hero renders only when the
deploy serves a free tier AND the provider exposes a hosted sign-in
URL, and clerk is the only provider that does. Dummy clerk env is
enough - every request in this suite is anonymous, and nothing
navigates to the (fake) sign-in host.

sw.js is stubbed to an empty no-fetch-handler worker: registration
succeeds quietly (no console noise for the zero-console-error pin)
while navigations and fetches keep hitting Playwright's routing.
The suite pins:

- The M2 loop: land -> hero -> generate -> the deck page for the
  stored deck -> study one card through the normal loop -> reload `/`
  and get the dashboard, with no uncaught page errors.
- The account is the cookie's: a fresh browser sees the landing, not
  the first browser's deck.
- Busy and day-limit error copy, input preserved, no account minted.
- The submit guard: two submit events in one task produce exactly one
  POST to the generation endpoint.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.e2e.celld_node import (
    LocalCelldNode,
    clerk_vars,
    llm_stub_env,
    open_limiter_vars,
)
from tests.e2e.conftest import new_iphone_context
from tests.e2e.test_offline_study_e2e import _idb_all, _wait_for

pytestmark = [pytest.mark.slow, pytest.mark.browser]

DECK_A = {
    "display_name": "World Capitals",
    "cards": [
        {"prompt": "Capital of Kenya?", "answer": "Nairobi", "answer_regex": "nairobi"},
        {"prompt": "Capital of Ghana?", "answer": "Accra", "answer_regex": "accra"},
        {"prompt": "Capital of Peru?", "answer": "Lima", "answer_regex": "lima"},
        {"prompt": "Capital of Japan?", "answer": "Tokyo", "answer_regex": "tokyo"},
        {
            "prompt": "What does the acronym SRS stand for?",
            "answer": "Spaced repetition system.",
            "answer_regex": None,
        },
    ],
}

BUSY_BODY = {
    "kind": "busy",
    "message": "The free AI is busy right now. Try again in a few minutes.",
}
DAY_LIMIT_BODY = {
    "kind": "rate_limited",
    "scope": "day",
    "message": "You've reached today's limit. Create a free account to keep going.",
    "retry_after_s": 3600,
}


class _StubInference:
    """An OpenAI-compatible `/chat/completions` that answers with a
    canned card array. Standing in for the deploy's free tier is what
    makes the real endpoint runnable here: the deck is generated,
    stored and served back for real, with nothing billed."""

    def __init__(self):
        self.deck = DECK_A
        self.prompts: list[str] = []
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's name
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length)
                try:
                    outer.prompts.append(json.loads(raw)["messages"][0]["content"])
                except Exception:  # noqa: BLE001 - the prompt log is diagnostic
                    outer.prompts.append("")
                content = json.dumps(
                    [
                        {"q": c["prompt"], "a": c["answer"], "r": c["answer_regex"]}
                        for c in outer.deck["cards"]
                    ]
                )
                body = json.dumps(
                    {
                        "choices": [{"message": {"content": content}}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def start(self):
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture(scope="session")
def stub_inference():
    stub = _StubInference()
    stub.start()
    try:
        yield stub
    finally:
        stub.stop()


@pytest.fixture(scope="session")
def instant_server(celld_build, stub_inference):
    """LOCAL node in the public deploy shape: clerk auth (anonymous
    visitors resolve to None and the provider exposes a sign-in URL
    for the hero gate + nudges) and a free tier pointed at the stub.

    The limiter is opened up because several tests generate for real
    from one client IP; the limiter's own behaviour is pinned by the
    route tests, which can control the clock."""
    node = LocalCelldNode(
        "instant",
        vars=open_limiter_vars(),
        script_env=llm_stub_env(stub_inference.base_url),
    )
    node.vars.update(clerk_vars(node.base_url))
    node.start()
    try:
        yield node
    finally:
        node.stop()


def _new_anon_context(browser_session):
    """Anonymous browser context: no identity headers of any kind, and
    a no-op sw.js so registration succeeds without a fetch handler
    that would bypass Playwright's routing."""
    ctx = new_iphone_context(browser_session)

    def _stub_sw(route):
        route.fulfill(
            status=200,
            content_type="application/javascript",
            body="// instant-e2e stub service worker: no fetch handler\n",
        )

    ctx.route("**/sw.js", _stub_sw)
    return ctx


@pytest.fixture()
def live_ctx(browser_session, instant_server, stub_inference):
    """A context that reaches the REAL generation endpoint. Yields
    (ctx, stub): set `stub.deck` to steer what the inference stub
    returns."""
    stub_inference.deck = DECK_A
    ctx = _new_anon_context(browser_session)
    try:
        yield ctx, stub_inference
    finally:
        ctx.close()


@pytest.fixture()
def instant_ctx(browser_session, instant_server):
    """A context whose generation endpoint is mocked, for the error
    branches where no deck is written. Yields (ctx, gen, sync_posts):
    mutate `gen` to steer the canned response; `sync_posts` records
    every POST to the sync endpoint so 'nothing synced' is a direct
    observation."""
    gen = {"status": 200, "body": DECK_A}
    sync_posts: list[str] = []
    ctx = _new_anon_context(browser_session)

    def _serve_generate(route):
        route.fulfill(
            status=gen["status"],
            content_type="application/json",
            body=json.dumps(gen["body"]),
        )

    def _count_sync_posts(route):
        sync_posts.append(route.request.post_data or "")
        route.fallback()

    ctx.route("**/api/offline/sync", _count_sync_posts)
    ctx.route("**/api/instant/generate", _serve_generate)
    try:
        yield ctx, gen, sync_posts
    finally:
        ctx.close()


def _watch_errors(page) -> tuple[list[str], list[str]]:
    """Collect console-error messages (with their source URL) and
    uncaught page errors from `page` for later assertion."""
    console_errors: list[str] = []
    page_errors: list[str] = []

    def _on_console(msg):
        if msg.type == "error":
            url = (msg.location or {}).get("url", "")
            console_errors.append(f"{msg.text} [{url}]")

    def _on_pageerror(err):
        page_errors.append(str(err))

    page.on("console", _on_console)
    page.on("pageerror", _on_pageerror)
    return console_errors, page_errors


def _unexpected_console(console_errors: list[str], base: str) -> list[str]:
    """Console errors this suite is not about.

    Two allowances: the anonymous landing's background snapshot probe
    401s by design (sync.js resolves identity on every online page
    load; a visitor has none), and a resource served from another
    origin (the webfont host) failing to load is the network's
    business, not this page's. Every other console error is a real
    finding, including a failed load of one of our own URLs."""
    kept = []
    for message in console_errors:
        if "/api/offline/snapshot" in message and "401" in message:
            continue
        source = message.rsplit("[", 1)[-1].rstrip("]")
        if source and not source.startswith(base):
            continue
        kept.append(message)
    return kept


def _goto_landing(page, base: str):
    page.goto(base + "/")
    hero = page.locator("[data-instant-start]")
    hero.wait_for()
    return hero


def _generate(page, topic: str) -> None:
    """Fill the hero form and submit. Click auto-waits for the button
    to leave its ships-disabled state (module init enables it)."""
    page.locator(".instant-form textarea").fill(topic)
    page.get_by_role("button", name="Generate my deck").click()


# ---- the M2 loop ---------------------------------------------------------


def test_anonymous_generate_lands_on_the_deck_and_studies(instant_server, live_ctx):
    """The milestone's own acceptance path: type a topic, land on
    `/deck/<slug>`, study a card through the normal loop, reload `/`
    and get the dashboard. Every screen after the hero is the same
    one a signed-in user gets; the only new browser state is the
    cookie."""
    ctx, stub = live_ctx
    stub.deck = DECK_A
    base = instant_server.base_url
    page = ctx.new_page()
    console_errors, page_errors = _watch_errors(page)
    instant_server.start()  # idempotent; heals a prior test's failure state

    # -- land: the hero IS the product ---------------------------------
    hero = _goto_landing(page, base)
    assert "What do you want to learn today?" in hero.locator("h1").inner_text()
    assert "Deck generation runs on a shared free AI service" in hero.inner_text()
    assert hero.get_attribute("data-sign-in-url")

    # -- generate: straight onto the stored deck's page -----------------
    _generate(page, "world capitals")
    page.wait_for_url("**/deck/**")
    slug = page.url.rsplit("/", 1)[-1]
    # The slug is opaque, not derived from the topic text.
    assert "capital" not in slug

    # The deck page is the ordinary one: display name from the topic,
    # every generated card present, all of them due.
    page.wait_for_selector(".deck-hero-cta")
    assert page.locator(".prelude h1, .deck-hero h1").first.inner_text() == "world capitals"
    body_text = page.locator("body").inner_text()
    for card in DECK_A["cards"]:
        assert card["prompt"] in body_text

    # Nothing device-local was written: the deck lives on the server.
    assert _idb_all(page, "local_cards") == []

    # -- study one card through the normal loop -------------------------
    page.get_by_role("button", name="Begin study session").click()
    page.wait_for_selector(".study-card textarea")
    prompt = page.locator(".study-prompt").inner_text()
    answer = {c["prompt"]: c["answer"] for c in DECK_A["cards"]}[prompt]
    page.locator(".study-card textarea").fill(answer)
    page.get_by_role("button", name="Submit").click()
    # Free text with no tier to fund a judge reveals and self-grades,
    # which is what an anonymous account always gets.
    page.wait_for_selector(".offline-selfgrade-blurb")
    page.get_by_role("button", name="I got it right").click()
    page.wait_for_selector("h1.verdict-headline")
    assert page.locator("h1.verdict-headline").inner_text() == "Right."

    # -- reload the landing URL: the dashboard, not the hero ------------
    page.goto(base + "/")
    page.wait_for_selector(".prelude h1")
    assert page.locator("[data-instant-start]").count() == 0
    assert "world capitals" in page.locator("body").inner_text()

    assert page_errors == [], page_errors
    assert _unexpected_console(console_errors, base) == [], console_errors


def test_forget_this_device_works_from_the_real_page(instant_server, live_ctx):
    """The route refuses a request the browser reports as cross-site,
    so the button a real browser presses has to keep working: a real
    same-origin form POST carries `Sec-Fetch-Site: same-origin`."""
    ctx, stub = live_ctx
    stub.deck = DECK_A
    base = instant_server.base_url
    page = ctx.new_page()
    instant_server.start()  # idempotent; heals a prior test's failure state

    _goto_landing(page, base)
    _generate(page, "world capitals")
    page.wait_for_url("**/deck/**")
    # The deck page refreshes the snapshot on load. Wait for that to land
    # before pressing the button: a refresh still in flight re-stamps the
    # flag after the wipe, and the landing then keeps rendering local decks.
    _wait_for(lambda: _idb_all(page, "cards"), message="the snapshot on the device")

    page.on("dialog", lambda dialog: dialog.accept())
    page.locator(".user-forget-form").wait_for(state="attached")
    page.evaluate("() => document.querySelector('.user-forget-form').requestSubmit()")

    # Cookie gone: the landing hero is back and the deck is not.
    page.wait_for_selector("[data-instant-start]")
    assert DECK_A["cards"][0]["prompt"] not in page.locator("body").inner_text()


def test_the_deck_belongs_to_the_cookie_not_the_deploy(instant_server, live_ctx, browser_session):
    """The account is the cookie's. A second browser holding no cookie
    still sees the landing hero, and the deck URL it can read off a
    shared link answers with the sign-in bounce, never the cards.

    The deck fetch goes through the API request context: a real
    navigation would follow the bounce to the deploy's (fake) hosted
    sign-in host and fail on DNS instead of on the assertion."""
    ctx, stub = live_ctx
    stub.deck = DECK_A
    base = instant_server.base_url
    instant_server.start()  # idempotent; heals a prior test's failure state

    page = ctx.new_page()
    _goto_landing(page, base)
    _generate(page, "world capitals")
    page.wait_for_url("**/deck/**")
    deck_url = page.url
    assert DECK_A["cards"][0]["prompt"] in page.locator("body").inner_text()

    other = _new_anon_context(browser_session)
    try:
        stranger = other.new_page()
        stranger.goto(base + "/")
        stranger.locator("[data-instant-start]").wait_for()

        response = other.request.get(deck_url, max_redirects=0)
        assert response.status in (303, 401)
        for card in DECK_A["cards"]:
            assert card["prompt"] not in response.text()
    finally:
        other.close()


# ---- error paths ---------------------------------------------------------


def test_busy_shows_inline_copy_and_preserves_input(instant_server, instant_ctx):
    ctx, gen, sync_posts = instant_ctx
    gen["status"] = 429
    gen["body"] = BUSY_BODY
    base = instant_server.base_url
    page = ctx.new_page()
    instant_server.start()  # idempotent; heals a prior test's failure state

    _goto_landing(page, base)
    _generate(page, "kernel scheduling")
    error = page.locator("[data-instant-error]")
    error.wait_for(state="visible")
    assert error.inner_text() == "The free AI is busy right now. Try again in a few minutes."
    # Input preserved, form still live, no retry storm (manual button).
    assert page.locator(".instant-form textarea").input_value() == "kernel scheduling"
    assert page.locator(".instant-form").is_visible()
    assert page.locator(".instant-form").is_visible()
    assert page.locator("[data-instant-status]").is_hidden()
    # No IDB writes on the error path.
    assert _idb_all(page, "local_cards") == []


def test_day_limit_copy_links_sign_in(instant_server, instant_ctx):
    ctx, gen, sync_posts = instant_ctx
    gen["status"] = 429
    gen["body"] = DAY_LIMIT_BODY
    base = instant_server.base_url
    page = ctx.new_page()
    instant_server.start()  # idempotent; heals a prior test's failure state

    hero = _goto_landing(page, base)
    sign_in_url = hero.get_attribute("data-sign-in-url")
    _generate(page, "roman emperors")
    error = page.locator("[data-instant-error]")
    error.wait_for(state="visible")
    assert (
        error.inner_text() == "You've reached today's limit. Create a free account to keep going."
    )
    link = error.get_by_role("link", name="Create a free account")
    assert link.get_attribute("href") == sign_in_url
    assert page.locator(".instant-form textarea").input_value() == "roman emperors"
    assert _idb_all(page, "local_cards") == []


# ---- submit guard --------------------------------------------------------


def test_double_submit_sends_one_generate_post(instant_server, live_ctx):
    """Two submit events landing in one task (double-tap, Enter plus
    click) must produce exactly one POST: the in-flight guard closes
    before submit()'s first await. Run against the real endpoint, so a
    second POST would also be a second deck and a second spend."""
    ctx, stub = live_ctx
    stub.deck = DECK_A
    base = instant_server.base_url
    page = ctx.new_page()
    instant_server.start()  # idempotent; heals a prior test's failure state

    generate_posts: list[str] = []

    def _count_generate(route):
        generate_posts.append(route.request.post_data or "")
        route.fallback()

    page.route("**/api/instant/generate", _count_generate)

    _goto_landing(page, base)
    page.locator(".instant-form textarea").fill("world capitals")
    page.wait_for_function("() => !document.querySelector('.instant-generate').disabled")
    page.evaluate(
        """() => {
          const form = document.querySelector("[data-instant-form]");
          form.requestSubmit();
          form.requestSubmit();
        }"""
    )
    page.wait_for_url("**/deck/**")
    page.wait_for_timeout(400)
    assert len(generate_posts) == 1
    # One account, one deck: the dashboard lists exactly what was asked for.
    page.goto(base + "/")
    page.wait_for_selector(".prelude h1")
    assert page.locator("body").inner_text().count("world capitals") == 1
