"""End-to-end instant-start landing + guest study surface
(docs/INSTANT-START.md sections 2 and 3.3, M4 scope).

Drives the anonymous client machine with ZERO upstream spend:
Playwright intercepts POST /api/instant/generate via ctx.route and
serves canned responses, so the suite exercises the landing form
states, the IndexedDB writes, the guest-mode /offline presentation,
and the returning-visitor / owner-present edges without an AI call.

The server is a LOCAL uvicorn booted in the PUBLIC deploy shape
(PREP_AUTH_MODE=clerk + free-tier env): the instant hero renders only
when the deploy serves a free tier AND the provider exposes a hosted
sign-in URL, and clerk mode is the only provider that does. Dummy
clerk env is enough - every request in this suite is anonymous, and
nothing navigates to the (fake) sign-in host.

sw.js is stubbed to an empty no-fetch-handler worker: registration
succeeds quietly (no console noise for the zero-console-error pin)
while navigations and fetches keep hitting Playwright's routing.
The suite pins:

- The full anonymous loop: land -> hero -> generate -> guest
  overview -> auto-grade a regex short ->
  self-verdict a null-regex card -> caught-up nudge banner -> footer
  disclosure line, with no uncaught page errors and no "Back online"
  banner anywhere in the session.
- Busy and day-limit error copy, input preserved, no IDB writes.
- The submit guard: two submit events in one task produce exactly one
  POST to the generation endpoint.
- The returning-visitor strip, the replace confirm, and the orphan
  outbox_reviews cleanup when a second generation replaces the deck.
- The owner-present edge: generation (first AND second, the replace
  branch must stay skipped) appends plain local_cards rows, never
  deletes existing ones, never writes meta.guest, never renders the
  strip or a confirm.
- The reconnect suppression: a guest shell boot with queued reviews
  never probes, never flushes, never shows the sync banner.
- Nudge dismissal: "Not now" persists across reloads for a guest with
  no meta.guest record (owner-absent local_cards only).
"""

from __future__ import annotations

import json

import pytest

from tests.e2e.conftest import LocalOfflineServer
from tests.e2e.test_offline_adoption_e2e import _SEED_GUEST_JS
from tests.e2e.test_offline_study_e2e import (
    _IDB_META_GET_JS,
    _idb_all,
    _module_prefix,
)

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

DECK_B = {
    "display_name": "Music Intervals",
    "cards": [
        {"prompt": "Semitones in a perfect fifth?", "answer": "7", "answer_regex": "7"},
        {"prompt": "Semitones in a major third?", "answer": "4", "answer_regex": "4"},
        {"prompt": "Semitones in an octave?", "answer": "12", "answer_regex": "12"},
        {"prompt": "Semitones in a perfect fourth?", "answer": "5", "answer_regex": "5"},
        {
            "prompt": "What is an interval in music?",
            "answer": "The pitch distance between two notes.",
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


@pytest.fixture(scope="session")
def instant_server(tmp_path_factory):
    """LOCAL server in the public deploy shape: clerk auth (anonymous
    visitors resolve to None and the provider exposes a sign-in URL
    for the hero gate + nudges) and a configured free tier (the other
    hero gate). The upstream is never reached - generation is
    route-mocked in the browser."""
    db_path = tmp_path_factory.mktemp("instant-e2e") / "data.sqlite"
    server = LocalOfflineServer(db_path)
    server.extra_env = {
        "PREP_AUTH_MODE": "clerk",
        "CLERK_SECRET_KEY": "sk_test_instant_e2e_dummy",
        "CLERK_AUTHORIZED_PARTIES": server.base_url,
        "CLERK_FRONTEND_API_URL": "https://accounts.example.test",
        "PREP_FREE_INFERENCE_BASE_URL": "https://inference.example.test/v1",
        "PREP_FREE_INFERENCE_API_KEY": "instant-e2e-test-key",
        "PREP_FREE_INFERENCE_MODEL": "instant-e2e-test-model",
    }
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture()
def instant_ctx(browser_session, instant_server):
    """Anonymous browser context: no identity headers of any kind.
    Yields (ctx, gen, sync_posts): mutate `gen` to steer what the
    mocked generation endpoint returns; `sync_posts` records every
    POST to the sync endpoint so 'nothing synced' is a direct
    observation."""
    gen = {"status": 200, "body": DECK_A}
    sync_posts: list[str] = []
    ctx = browser_session.new_context(
        user_agent=(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.4 Mobile/15E148 Safari/604.1"
        ),
        viewport={"width": 393, "height": 852},
        device_scale_factor=3,
        is_mobile=True,
        has_touch=True,
    )
    ctx.set_default_timeout(15_000)
    ctx.set_default_navigation_timeout(15_000)

    def _serve_generate(route):
        route.fulfill(
            status=gen["status"],
            content_type="application/json",
            body=json.dumps(gen["body"]),
        )

    def _count_sync_posts(route):
        sync_posts.append(route.request.post_data or "")
        route.fallback()

    def _stub_sw(route):
        # A no-op worker: registration succeeds (no console noise),
        # but with no fetch handler every request keeps flowing
        # through Playwright's routing.
        route.fulfill(
            status=200,
            content_type="application/javascript",
            body="// instant-e2e stub service worker: no fetch handler\n",
        )

    ctx.route("**/sw.js", _stub_sw)
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


def _unexpected_console(console_errors: list[str]) -> list[str]:
    """Strip the one legitimate noise line: the anonymous landing's
    background snapshot probe 401s by design (sync.js resolves
    identity on every online page load; a visitor has none). Every
    other console error is a real finding."""
    return [m for m in console_errors if not ("/api/offline/snapshot" in m and "401" in m)]


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


# ---- the full anonymous loop -------------------------------------------


def test_full_anonymous_loop_generate_study_nudge(instant_server, instant_ctx):
    ctx, gen, sync_posts = instant_ctx
    gen["body"] = DECK_A
    base = instant_server.base_url
    page = ctx.new_page()
    console_errors, page_errors = _watch_errors(page)
    instant_server.start()  # idempotent; heals a prior test's failure state

    # -- land: the hero IS the product ---------------------------------
    hero = _goto_landing(page, base)
    assert "What do you want to learn today?" in hero.locator("h1").inner_text()
    assert "Deck generation runs on a shared free AI service" in hero.inner_text()
    sign_in_url = hero.get_attribute("data-sign-in-url")
    assert sign_in_url

    # -- generate (mocked): straight into the deck ---------------------
    # No summary screen: generating navigates to the study surface, so
    # the deck view the user lands on is the only deck view there is.
    _generate(page, "world capitals")
    page.wait_for_url("**/offline")

    # -- IDB truth: guest deck written, owner untouched ----------------
    cards = _idb_all(page, "local_cards")
    assert len(cards) == 5
    by_prompt = {c["prompt"]: c for c in cards}
    assert set(by_prompt) == {c["prompt"] for c in DECK_A["cards"]}
    for spec_card in DECK_A["cards"]:
        row = by_prompt[spec_card["prompt"]]
        assert row["type"] == "short"
        assert row["deck_name"] == "World Capitals"
        assert row["answer_regex"] == spec_card["answer_regex"]
        assert row["deck_id"] is None
        assert row["local_step"] == 0
        assert row["local_next_due"] is None
    guest = page.evaluate(_IDB_META_GET_JS, "guest")
    assert guest["display_name"] == "World Capitals"
    assert guest["topic"] == "world capitals"
    assert page.evaluate(_IDB_META_GET_JS, "owner") is None

    # -- the guest overview --------------------------------------------
    page.wait_for_selector(".offline-deck-list")
    assert page.locator(".prelude .eyebrow").inner_text() == "Your deck"
    assert page.locator(".prelude h1").inner_text() == "World Capitals"
    assert "5 cards are due right now." in page.locator(".lede").inner_text()
    # Guest mode reads as the app: plain brand, no "offline" suffix,
    # and the brand links back to the landing (the guest's only way
    # out of the study surface).
    assert page.locator(".masthead .brand-word").inner_text() == "Prep"
    assert page.locator("a.masthead-brand").get_attribute("href").endswith("/")
    # The one guest deck renders from meta.guest with live counts.
    assert "5 due · 5 cards" in page.locator(".offline-deck-list").inner_text()
    # Persistent disclosure line instead of the snapshot stamp.
    note = page.locator(".offline-guest-note")
    assert "This deck is stored only on this device." in note.inner_text()
    assert note.get_by_role("link", name="Create an account").get_attribute("href") == (
        page.locator("#offline-root").get_attribute("data-sign-in-url")
    )
    assert page.locator(".offline-snapshot-stamp").count() == 0

    # -- study all five: regex shorts auto-grade, the null-regex card
    #    reveals + self-verdicts. Queue order is shuffled within the
    #    due bucket, so each card is identified by its prompt. --------
    spec_by_prompt = {c["prompt"]: c for c in DECK_A["cards"]}
    auto_graded = 0
    self_graded = 0
    page.get_by_role("button", name="Study").click()
    for _ in range(5):
        page.wait_for_selector(".study-card textarea")
        prompt = page.locator(".study-prompt").inner_text()
        card = spec_by_prompt[prompt]
        page.locator(".study-card textarea").fill(card["answer"])
        page.get_by_role("button", name="Submit").click()
        if card["answer_regex"]:
            auto_graded += 1
        else:
            page.wait_for_selector(".offline-selfgrade-blurb")
            page.get_by_role("button", name="I got it right").click()
            self_graded += 1
        page.wait_for_selector("h1.verdict-headline")
        assert page.locator("h1.verdict-headline").inner_text() == "Right."
        # For a guest the ladder IS the schedule: no divergence
        # qualifier on the verdict.
        assert "offline schedule" not in page.locator(".verdict-sub").inner_text()
        page.get_by_role("button", name="Next card").click()
    assert auto_graded == 4
    assert self_graded == 1

    outbox = _idb_all(page, "outbox_reviews")
    assert len(outbox) == 5
    assert all(r["card_client_id"] for r in outbox)
    assert sorted(r["graded_by"] for r in outbox) == ["auto"] * 4 + ["self"]

    # -- caught up: the post-session nudge banner ----------------------
    page.wait_for_selector(".offline-guest-nudge")
    nudge = page.locator(".offline-guest-nudge")
    assert (
        "Nice work. This deck lives only on this device so far. "
        "Create a free account to keep it and study anywhere." in nudge.inner_text()
    )
    cta = nudge.get_by_role("link", name="Create a free account")
    assert cta.get_attribute("href") == page.locator("#offline-root").get_attribute(
        "data-sign-in-url"
    )
    assert nudge.get_by_role("button", name="Not now").is_visible()
    assert "All caught up" in page.locator(".empty-state").inner_text()
    # Never a modal, never over a card.
    assert page.locator("dialog[open]").count() == 0

    # -- back to overview: footer line persists, banner never shown ----
    page.get_by_role("button", name="Back to overview").click()
    page.wait_for_selector(".offline-guest-note")
    assert "Nothing is due right now." in page.locator(".lede").inner_text()
    # The reconnect sync banner never appeared during the guest
    # session (pins the guest no-op in syncOnReconnect) and nothing
    # was POSTed to the sync endpoint.
    assert page.locator(".offline-banner").count() == 0
    assert sync_posts == []

    # -- browser health: no uncaught errors, no unexplained console ----
    assert page_errors == [], page_errors
    assert _unexpected_console(console_errors) == [], console_errors


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
    assert page.evaluate(_IDB_META_GET_JS, "guest") is None


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
    assert page.evaluate(_IDB_META_GET_JS, "guest") is None


# ---- submit guard --------------------------------------------------------


def test_double_submit_sends_one_generate_post(instant_server, instant_ctx):
    """Two submit events landing in one task (double-tap, Enter plus
    click) must produce exactly one POST: the in-flight guard closes
    before submit()'s first await."""
    ctx, gen, sync_posts = instant_ctx
    gen["body"] = DECK_A
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
    page.wait_for_url("**/offline")
    page.wait_for_timeout(400)
    assert len(generate_posts) == 1
    # One write pass too: the deck landed once.
    assert len(_idb_all(page, "local_cards")) == 5


# ---- returning visitor -----------------------------------------------------

# Queue one review against the first generated card, the shape
# recordVerdict writes (card_client_id key). The replace must take
# this orphan-to-be with it.
_QUEUE_GUEST_REVIEW_JS = """
async ({prefix}) => {
  const store = await import(prefix + "offline/store.js");
  const cards = await store.getAll("local_cards");
  const target = cards[0];
  await store.withLock(() =>
    store.put("outbox_reviews", {
      client_id: store.uuid(),
      card_client_id: target.client_id,
      verdict: "right",
      user_answer: target.answer,
      graded_by: "auto",
      reviewed_at: new Date().toISOString(),
    })
  );
  return target.client_id;
}
"""


def test_returning_visitor_strip_and_replace_confirm(instant_server, instant_ctx):
    ctx, gen, sync_posts = instant_ctx
    gen["body"] = DECK_A
    base = instant_server.base_url
    page = ctx.new_page()
    instant_server.start()  # idempotent; heals a prior test's failure state

    # -- first visit: generate deck A, queue one review ----------------
    _goto_landing(page, base)
    _generate(page, "world capitals")
    page.wait_for_url("**/offline")
    prefix = _module_prefix(page)
    page.evaluate(_QUEUE_GUEST_REVIEW_JS, {"prefix": prefix})
    assert len(_idb_all(page, "outbox_reviews")) == 1

    # -- reload: the Continue-studying strip ----------------------------
    page.goto(base + "/")
    strip = page.locator("[data-instant-continue]")
    strip.wait_for(state="visible")
    # inner_text reflects rendered text and the eyebrow is
    # CSS-uppercased; compare case-insensitively.
    strip_text = strip.inner_text()
    assert "continue studying" in strip_text.lower()
    assert "World Capitals" in strip_text
    assert "5 cards · 5 due" in strip_text
    assert strip.get_by_role("link").get_attribute("href").endswith("/offline")

    # -- second generation: one confirm, then replace -------------------
    confirms: list[str] = []

    def _on_dialog(dialog):
        confirms.append(dialog.message)
        dialog.accept()

    page.on("dialog", _on_dialog)
    gen["body"] = DECK_B
    _generate(page, "music intervals")
    page.wait_for_url("**/offline")
    assert confirms == [
        "Replace your current deck (World Capitals)? It's only stored on this device."
    ]
    # Landed on the replaced deck, not a summary of it.
    page.wait_for_selector(".offline-deck-list")
    assert page.locator(".prelude h1").inner_text() == "Music Intervals"

    # The replace swapped every card, updated meta.guest, and took the
    # replaced cards' outbox_reviews rows with it (an orphan review
    # would surface as a reject at adoption time).
    cards = _idb_all(page, "local_cards")
    assert {c["prompt"] for c in cards} == {c["prompt"] for c in DECK_B["cards"]}
    assert page.evaluate(_IDB_META_GET_JS, "guest")["display_name"] == "Music Intervals"
    assert _idb_all(page, "outbox_reviews") == []
    assert sync_posts == []


# ---- owner-present device ---------------------------------------------------

_SEED_OWNER_JS = """
async ({prefix}) => {
  const store = await import(prefix + "offline/store.js");
  await store.withLock(() =>
    store.metaPut("owner", {
      user_id: "owner-device@example.com",
      display_name: "Device Owner",
      snapshot_at: new Date().toISOString(),
      build: null,
    })
  );
}
"""


def test_owner_present_device_appends_without_guest_state(instant_server, instant_ctx):
    """Spec edge table: on a device with an owner snapshot the landing
    input still works, generated cards are ordinary authored
    local_cards rows, meta.guest is NEVER written, no replace confirm
    fires, and no Continue-studying strip renders on the next visit."""
    ctx, gen, sync_posts = instant_ctx
    gen["body"] = DECK_A
    base = instant_server.base_url
    page = ctx.new_page()
    instant_server.start()  # idempotent; heals a prior test's failure state

    # Seed the owner through the un-auth-gated shell (no sync runs
    # there for a device with no due data).
    page.goto(base + "/offline")
    page.evaluate(_SEED_OWNER_JS, {"prefix": _module_prefix(page)})
    assert page.evaluate(_IDB_META_GET_JS, "owner")["user_id"] == "owner-device@example.com"

    confirms: list[str] = []

    def _on_dialog(dialog):
        confirms.append(dialog.message)
        dialog.accept()

    page.on("dialog", _on_dialog)

    _goto_landing(page, base)
    _generate(page, "world capitals")
    page.wait_for_url("**/offline")

    # No confirm (there is no guest deck to replace), no guest record;
    # the cards append to the owner's local state and ride the normal
    # silent flush next time that owner signs in.
    assert confirms == []
    assert page.evaluate(_IDB_META_GET_JS, "guest") is None
    cards = _idb_all(page, "local_cards")
    assert {c["prompt"] for c in cards} == {c["prompt"] for c in DECK_A["cards"]}
    assert all(c["deck_name"] == "World Capitals" for c in cards)
    owner = page.evaluate(_IDB_META_GET_JS, "owner")
    assert owner["user_id"] == "owner-device@example.com"

    # Next visit: module init done (button enabled) and the strip
    # stays hidden; no adoption dialog either (owner present is state
    # 3, not state 2).
    page.goto(base + "/")
    page.wait_for_function("() => !document.querySelector('.instant-generate').disabled")
    page.wait_for_timeout(400)
    assert page.locator("[data-instant-continue]").is_hidden()
    assert page.locator("dialog.offline-adoption-dialog").count() == 0
    assert sync_posts == []

    # Second generation on the owner device: the replace branch must
    # stay skipped. Still no confirm, no meta.guest, and the first
    # deck's cards survive (a regression that deletes unconditionally
    # would wipe them).
    gen["body"] = DECK_B
    _generate(page, "music intervals")
    page.wait_for_url("**/offline")
    assert confirms == []
    assert page.evaluate(_IDB_META_GET_JS, "guest") is None
    cards = _idb_all(page, "local_cards")
    assert {c["prompt"] for c in cards} == (
        {c["prompt"] for c in DECK_A["cards"]} | {c["prompt"] for c in DECK_B["cards"]}
    )


# ---- reconnect suppression ---------------------------------------------------


def test_guest_shell_boot_never_shows_sync_banner(instant_server, instant_ctx):
    """A guest with queued reviews boots the shell: syncOnReconnect
    must return before probing or flushing (no account to sync with
    until adoption), so no "Back online" banner, no /healthz probe,
    no sync POST - on boot AND on a later online event."""
    ctx, gen, sync_posts = instant_ctx
    base = instant_server.base_url
    healthz_probes: list[str] = []

    def _count_healthz(route):
        healthz_probes.append(route.request.url)
        route.fallback()

    ctx.route("**/healthz", _count_healthz)
    page = ctx.new_page()
    instant_server.start()  # idempotent; heals a prior test's failure state

    # Seed guest data + one queued review directly (the exact shape
    # instant-start generation and a study session write).
    page.goto(base + "/offline")
    prefix = _module_prefix(page)
    page.evaluate(
        _SEED_GUEST_JS,
        {
            "prefix": prefix,
            "deckName": "Guest Capitals",
            "kenyaPrompt": "Capital of Kenya?",
            "ghanaPrompt": "Capital of Ghana?",
        },
    )

    # Boot with the guest data present: guest overview renders WITHOUT
    # the account-flavored waiting-to-sync notes, and the reconnect
    # path stays silent.
    page.goto(base + "/offline")
    page.wait_for_selector(".offline-deck-list")
    assert "2 cards are due right now." in page.locator(".lede").inner_text()
    assert "waiting to sync" not in page.locator("#offline-root").inner_text()
    page.wait_for_timeout(1_500)
    assert page.locator(".offline-banner").count() == 0
    assert healthz_probes == []
    assert sync_posts == []

    # A connectivity event after boot takes the same guest no-op path.
    page.evaluate("() => window.dispatchEvent(new Event('online'))")
    page.wait_for_timeout(800)
    assert page.locator(".offline-banner").count() == 0
    assert healthz_probes == []
    assert sync_posts == []


# ---- nudge dismissal --------------------------------------------------------

# One owner-absent local_cards row in the M4 authoring shape (no type,
# no answer_regex, no deck_name): guest data with NO meta.guest record.
_SEED_AUTHORED_CARD_JS = """
async ({prefix, prompt}) => {
  const store = await import(prefix + "offline/store.js");
  await store.withLock(() =>
    store.put("local_cards", {
      client_id: store.uuid(),
      deck_id: null,
      prompt,
      answer: "Forty-two",
      created_at: new Date().toISOString(),
      local_step: 0,
      local_next_due: null,
    })
  );
}
"""


def _study_single_card_to_caught_up(page):
    """Drive the one due card (reveal + self-verdict shape) through to
    the caught-up screen."""
    page.get_by_role("button", name="Study").click()
    page.wait_for_selector(".study-card textarea")
    page.locator(".study-card textarea").fill("Forty-two")
    page.get_by_role("button", name="Submit").click()
    page.wait_for_selector(".offline-selfgrade-blurb")
    page.get_by_role("button", name="I got it right").click()
    page.wait_for_selector("h1.verdict-headline")
    page.get_by_role("button", name="Next card").click()
    page.wait_for_selector(".empty-state")


def test_nudge_dismiss_persists_without_guest_record(instant_server, instant_ctx):
    """ "Not now" must survive a reload even when the guest has no
    meta.guest record (owner-absent local_cards only): the dismissal
    persists under meta.guest_nudge, so a later caught-up screen never
    re-shows the banner."""
    ctx, gen, sync_posts = instant_ctx
    base = instant_server.base_url
    page = ctx.new_page()
    instant_server.start()  # idempotent; heals a prior test's failure state

    page.goto(base + "/offline")
    prefix = _module_prefix(page)
    page.evaluate(_SEED_AUTHORED_CARD_JS, {"prefix": prefix, "prompt": "Meaning of life?"})

    # Boot as an authored-only guest and study to caught-up: the nudge
    # banner renders, and "Not now" removes it.
    page.goto(base + "/offline")
    page.wait_for_selector(".offline-deck-list")
    assert page.evaluate(_IDB_META_GET_JS, "guest") is None
    _study_single_card_to_caught_up(page)
    page.wait_for_selector(".offline-guest-nudge")
    page.get_by_role("button", name="Not now").click()
    page.wait_for_selector(".offline-guest-nudge", state="detached")
    dismissed = page.evaluate(_IDB_META_GET_JS, "guest_nudge")
    assert dismissed and dismissed["dismissed_at"]
    assert page.evaluate(_IDB_META_GET_JS, "guest") is None

    # A fresh due card, a reload, another caught-up: the banner stays
    # suppressed by the persisted dismissal.
    page.evaluate(_SEED_AUTHORED_CARD_JS, {"prefix": prefix, "prompt": "Second question?"})
    page.goto(base + "/offline")
    page.wait_for_selector(".offline-deck-list")
    _study_single_card_to_caught_up(page)
    assert "All caught up" in page.locator(".empty-state").inner_text()
    assert page.locator(".offline-guest-nudge").count() == 0
    assert sync_posts == []
