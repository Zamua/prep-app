"""One recorded flow: an anonymous visitor builds a solar-system deck,
studies it, signs in, and finds the deck on the account.

The four steps are the ones a first-time visitor actually walks
(docs/ANONYMOUS-ACCOUNTS.md, docs/INSTANT-START.md): the splash with
nothing in the browser, a deck generated and stored under a freshly
minted anonymous account, a card studied through the normal loop, then
sign-in, on which the server merges the anonymous account into the
signed-in one. Every step is screenshotted through FlowRecorder, so the
artifact directory reads as the flow rather than as a pass/fail line.

Two harness details carry the whole file:

- The identity provider changes between step 3 and step 4. The splash
  under test is the instant hero, which renders only when the deploy
  has a free tier AND the provider exposes a hosted sign-in URL, and
  clerk is the only provider that does; a clerk session cannot be
  minted offline, while a tailscale login is a request header. So the
  same server, over the same sqlite and the same port, is restarted in
  tailscale mode to receive the sign-in. The merge itself is
  provider-agnostic by construction (prep/auth/identity.py runs it off
  the cookie after the upsert), so nothing under test is skipped.
- A stub OpenAI-compatible endpoint stands in for the free tier, so
  the deck is genuinely generated, stored and served back with no
  upstream spend.

Service workers are blocked at the context, not by routing sw.js: a
controlling worker re-issues navigations outside Playwright's routing,
and the sign-in here IS an injected header on a navigation. Routing
the script cannot prevent that, because the worker fetches its own
script outside that routing too.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.e2e.conftest import LocalOfflineServer
from tests.e2e.flow_artifacts import FlowRecorder
from tests.e2e.test_instant_start_e2e import _StubInference
from tests.e2e.test_offline_study_e2e import _wait_for

pytestmark = [pytest.mark.slow, pytest.mark.browser]

TOPIC = "the solar system"
SIGN_IN_LOGIN = "solar-flow-e2e@example.com"
SIGN_IN_NAME = "Solar Tester"

SOLAR_DECK = {
    "display_name": "The Solar System",
    "cards": [
        {"prompt": "Which planet is closest to the Sun?", "answer": "Mercury"},
        {"prompt": "Which planet has the Great Red Spot?", "answer": "Jupiter"},
        {"prompt": "Which planet has the brightest ring system?", "answer": "Saturn"},
        {"prompt": "Which planet is called the red planet?", "answer": "Mars"},
        {"prompt": "How many planets orbit the Sun?", "answer": "Eight"},
    ],
}

_ANSWERS = {c["prompt"]: c["answer"] for c in SOLAR_DECK["cards"]}


@pytest.fixture(scope="module")
def solar_stub():
    """This file's own inference stub, so the deck it serves is not
    shared mutable state with any other suite in the session."""
    stub = _StubInference()
    stub.deck = {
        "display_name": SOLAR_DECK["display_name"],
        "cards": [{**c, "answer_regex": None} for c in SOLAR_DECK["cards"]],
    }
    stub.start()
    try:
        yield stub
    finally:
        stub.stop()


@pytest.fixture(scope="module")
def solar_server(tmp_path_factory, solar_stub):
    """A local prep in the PUBLIC deploy shape, over a scratch sqlite.

    The limiter is opened up because the flow generates for real from
    the one runner IP; the limiter's own behaviour is pinned by the
    route tests, which can control the clock."""
    db_path = tmp_path_factory.mktemp("flow-solar") / "data.sqlite"
    server = LocalOfflineServer(db_path)
    server.extra_env = {
        "PREP_AUTH_MODE": "clerk",
        "CLERK_SECRET_KEY": "sk_test_solar_flow_e2e_dummy",
        "CLERK_AUTHORIZED_PARTIES": server.base_url,
        "CLERK_FRONTEND_API_URL": "https://accounts.example.test",
        "PREP_FREE_INFERENCE_BASE_URL": solar_stub.base_url,
        "PREP_FREE_INFERENCE_API_KEY": "solar-flow-e2e-test-key",
        "PREP_FREE_INFERENCE_MODEL": "solar-flow-e2e-test-model",
        "PREP_INSTANT_BURST_LIMIT": "100",
        "PREP_INSTANT_PER_IP_PER_DAY": "100",
        "PREP_INSTANT_GLOBAL_PER_MINUTE": "100",
        "PREP_INSTANT_GLOBAL_PER_DAY": "500",
    }
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture()
def solar_ctx(browser_session, solar_server):
    """Anonymous browser context whose identity is a mutable dict.

    Filling `identity` and reloading IS the sign-in."""
    identity: dict[str, str] = {}
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
        service_workers="block",
    )
    ctx.set_default_timeout(15_000)
    ctx.set_default_navigation_timeout(15_000)
    base = solar_server.base_url

    def _route(route, request):
        if identity and request.url.startswith(base):
            route.continue_(
                headers={
                    **request.headers,
                    "tailscale-user-login": identity["login"],
                    "tailscale-user-name": identity["name"],
                }
            )
            return
        route.continue_()

    ctx.route("**/*", _route)
    try:
        yield ctx, identity
    finally:
        ctx.close()


def _restart_in_tailscale_mode(server: LocalOfflineServer) -> None:
    """Same sqlite, same port, same free tier: only the identity
    provider changes. Dropping PREP_AUTH_MODE is enough, since start()
    strips it from the inherited environment and tailscale is the
    default."""
    server.stop()
    server.extra_env = {
        k: v
        for k, v in server.extra_env.items()
        if k != "PREP_AUTH_MODE" and not k.startswith("CLERK_")
    }
    server.start()


def _anon_login(db_path) -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT tailscale_login FROM users WHERE is_anonymous = 1").fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _deck_state(db_path, slug: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        deck = conn.execute("SELECT id, user_id, display_name FROM decks WHERE name = ?", (slug,))
        deck = deck.fetchone()
        if deck is None:
            return {}
        questions = conn.execute(
            "SELECT id, user_id FROM questions WHERE deck_id = ?", (deck["id"],)
        ).fetchall()
        reviews = conn.execute(
            "SELECT COUNT(*) AS n FROM reviews WHERE question_id IN"
            " (SELECT id FROM questions WHERE deck_id = ?)",
            (deck["id"],),
        ).fetchone()["n"]
        merges = conn.execute(
            "SELECT status, anon_user_id, target_user_id FROM account_merges ORDER BY id"
        ).fetchall()
        return {
            "deck_owner": deck["user_id"],
            "display_name": deck["display_name"],
            "question_owners": {q["user_id"] for q in questions},
            "questions": len(questions),
            "reviews": reviews,
            "anon_rows": conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE is_anonymous = 1"
            ).fetchone()["n"],
            "merges": [dict(m) for m in merges],
        }
    finally:
        conn.close()


# Every surface here rises in over 500ms, and the card list staggers
# on top of that, so a shot taken on the selector alone catches a
# half-painted page. Infinite animations (spinners) are excluded or
# the wait could never finish.
_SETTLED_JS = """
() => document.getAnimations().every(
  (a) => a.playState !== "running"
      || a.effect.getComputedTiming().iterations === Infinity
)
"""


def _shot(rec: FlowRecorder, page, label: str) -> None:
    page.wait_for_function(_SETTLED_JS)
    rec.shot(page, label)


def test_anonymous_solar_deck_survives_signing_in(solar_server, solar_ctx):
    """Splash to merged account in one session: the deck a visitor
    generated and studied with no account is on the account they sign
    in to, with the review they left on it."""
    ctx, identity = solar_ctx
    base = solar_server.base_url
    solar_server.start()  # idempotent; heals a prior test's failure state
    page = ctx.new_page()
    rec = FlowRecorder("anonymous-merge-solar-system")

    # -- 1. the splash, with nothing in the browser --------------------
    page.goto(base + "/")
    hero = page.locator("[data-instant-start]")
    hero.wait_for()
    assert "What do you want to learn today?" in hero.locator("h1").inner_text()
    assert [c for c in ctx.cookies() if c["name"] == "prep_anon"] == []
    _shot(rec, page, "splash")

    # -- 2. create a deck ----------------------------------------------
    page.locator(".instant-form textarea").fill(TOPIC)
    page.get_by_role("button", name="Generate my deck").click()
    page.wait_for_url("**/deck/**")
    slug = page.url.rsplit("/", 1)[-1]
    page.wait_for_selector(".deck-hero-cta")
    assert page.locator(".prelude h1, .deck-hero h1").first.inner_text() == TOPIC
    deck_text = page.locator("body").inner_text()
    for card in SOLAR_DECK["cards"]:
        assert card["prompt"] in deck_text
    _shot(rec, page, "deck-created")

    anon_id = _anon_login(solar_server.db_path)
    assert anon_id and anon_id.startswith("anon:")
    assert _deck_state(solar_server.db_path, slug)["deck_owner"] == anon_id

    # -- 3. study it ----------------------------------------------------
    page.get_by_role("button", name="Begin study session").click()
    page.wait_for_selector(".study-card textarea")
    prompt = page.locator(".study-prompt").inner_text()
    page.locator(".study-card textarea").fill(_ANSWERS[prompt])
    _shot(rec, page, "studying")
    page.get_by_role("button", name="Submit").click()
    # An anonymous account has no tier to fund a judge, so free text
    # reveals and self-grades.
    page.wait_for_selector(".offline-selfgrade-blurb")
    page.get_by_role("button", name="I got it right").click()
    page.wait_for_selector("h1.verdict-headline")
    assert page.locator("h1.verdict-headline").inner_text() == "Right."
    _shot(rec, page, "study-verdict")

    _wait_for(
        lambda: _deck_state(solar_server.db_path, slug)["reviews"] == 1,
        message="the anonymous review to reach the server",
    )

    # -- 4. sign in: the merge runs on this request ---------------------
    _restart_in_tailscale_mode(solar_server)
    identity.update({"login": SIGN_IN_LOGIN, "name": SIGN_IN_NAME})
    page.goto(base + "/")
    page.wait_for_selector(".prelude h1")

    _wait_for(
        lambda: _deck_state(solar_server.db_path, slug).get("deck_owner") == SIGN_IN_LOGIN,
        message="the merge to move the deck onto the signed-in account",
    )
    state = _deck_state(solar_server.db_path, slug)
    assert state["deck_owner"] == SIGN_IN_LOGIN, "the anonymous deck did not move"
    assert state["question_owners"] == {SIGN_IN_LOGIN}
    assert state["questions"] == len(SOLAR_DECK["cards"])
    assert state["reviews"] == 1, "the review left while anonymous was lost"
    assert state["anon_rows"] == 0
    # One navigation issues several authenticated requests, and every
    # one in flight when the merge lands still carries the cookie: the
    # extra attempts find no anonymous row and record it. Exactly one
    # of them moved data.
    statuses = [m["status"] for m in state["merges"]]
    assert statuses.count("completed") == 1
    assert set(statuses) <= {"completed", "failed"}
    assert {m["target_user_id"] for m in state["merges"]} == {SIGN_IN_LOGIN}
    assert {m["anon_user_id"] for m in state["merges"]} == {anon_id}

    # The signed-in dashboard, not the splash, and the deck is on it.
    assert page.locator("[data-instant-start]").count() == 0
    assert TOPIC in page.locator("body").inner_text()
    _shot(rec, page, "signed-in-dashboard")

    # Same deck URL, now served to the account.
    page.goto(f"{base}/deck/{slug}")
    page.wait_for_selector(".deck-hero-cta")
    merged_text = page.locator("body").inner_text()
    for card in SOLAR_DECK["cards"]:
        assert card["prompt"] in merged_text
    _shot(rec, page, "merged-deck")

    rec.manifest()
