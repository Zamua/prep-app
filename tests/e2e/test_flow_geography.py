"""One visitor, end to end: anonymous splash, deck, study, sign-in.

The flow this file records, in the order a first-time visitor meets
it: land on the splash with no identity, generate a world-geography
deck from the hero, study a card through the normal loop, then sign in
and find that deck on the account. Every step is screenshotted into
$PREP_E2E_ARTIFACTS (see tests/e2e/flow_artifacts.py).

The two halves are gated by different things, so the server changes
shape between them. The instant hero renders only where a free tier is
configured AND the provider publishes a sign-in URL, which is the
clerk shape; a Clerk session cannot be minted without Clerk, while a
header provider's identity can be injected per request. So signing in
is done by restarting the deploy onto the SAME port and the SAME
database with the header provider. The browser keeps its origin, its
`prep_anon` cookie and its IndexedDB across that, and the cookie is
the only thing the merge reads.

Inference is a local stub standing in for the deploy's free tier: the
deck is generated, stored and served back for real, with nothing
billed.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from tests.e2e.conftest import LocalOfflineServer
from tests.e2e.flow_artifacts import FlowRecorder
from tests.e2e.test_instant_start_e2e import _StubInference

pytestmark = [pytest.mark.slow, pytest.mark.browser]

TOPIC = "world geography: capital cities"
SIGNED_IN_LOGIN = "geography-flow@example.com"
SIGNED_IN_NAME = "Geography Flow"

# Five cards is the free tier's per-generation cap, so this is a whole
# generation rather than a truncated one.
GEOGRAPHY_DECK = {
    "display_name": "World geography",
    "cards": [
        {"prompt": "Capital of Australia?", "answer": "Canberra", "answer_regex": "canberra"},
        {"prompt": "Capital of Canada?", "answer": "Ottawa", "answer_regex": "ottawa"},
        {"prompt": "Capital of Turkey?", "answer": "Ankara", "answer_regex": "ankara"},
        {"prompt": "Capital of New Zealand?", "answer": "Wellington", "answer_regex": "wellington"},
        {"prompt": "Capital of Morocco?", "answer": "Rabat", "answer_regex": "rabat"},
    ],
}
ANSWERS = {card["prompt"]: card["answer"] for card in GEOGRAPHY_DECK["cards"]}


@pytest.fixture(scope="module")
def geography_stub():
    stub = _StubInference()
    stub.deck = GEOGRAPHY_DECK
    stub.start()
    try:
        yield stub
    finally:
        stub.stop()


@pytest.fixture(scope="module")
def flow_server(tmp_path_factory, geography_stub):
    """Local prep in the public deploy shape: clerk provider (so the
    landing renders the instant hero) plus a free tier pointed at the
    stub. Dummy clerk env is enough - the anonymous half never reaches
    Clerk, and the signed-in half runs on the header provider.

    The limiter is opened up because the whole flow generates from one
    client IP; its own behaviour is pinned by the route tests."""
    db_path = tmp_path_factory.mktemp("flow-geography") / "data.sqlite"
    server = LocalOfflineServer(db_path)
    server.extra_env = {
        "PREP_AUTH_MODE": "clerk",
        "CLERK_SECRET_KEY": "sk_test_flow_geography_dummy",
        "CLERK_AUTHORIZED_PARTIES": server.base_url,
        "CLERK_FRONTEND_API_URL": "https://accounts.example.test",
        "PREP_FREE_INFERENCE_BASE_URL": geography_stub.base_url,
        "PREP_FREE_INFERENCE_API_KEY": "flow-geography-test-key",
        "PREP_FREE_INFERENCE_MODEL": "flow-geography-test-model",
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
def flow_ctx(browser_session, flow_server):
    """One browser for the whole flow: the anonymous cookie has to
    survive into the signed-in half, which is what the merge runs off.
    Yields (ctx, identity); `identity` is empty until the sign-in step
    fills it, and every same-origin request carries whatever it holds.

    Service workers are blocked at the context, not by routing
    `/sw.js`: the registration request does not go through page
    routing, so a route on it installs the worker anyway. It has to be
    off, because the flow signs in and then NAVIGATES, and a
    controlling worker re-issues navigations outside Playwright's
    routing - they arrive with no identity header and the merge never
    runs."""
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
    base = flow_server.base_url
    identity: dict[str, str] = {}

    def _inject_identity(route, request):
        if identity and request.url.startswith(base):
            route.continue_(headers={**request.headers, **identity})
        else:
            route.continue_()

    ctx.route("**/*", _inject_identity)
    try:
        yield ctx, identity
    finally:
        ctx.close()


def _sign_in(server: LocalOfflineServer, identity: dict[str, str]) -> None:
    """Swap the deploy's identity provider without moving the port or
    the database, then start sending the header the new provider reads.
    The browser keeps everything it holds, exactly as it would across a
    real hosted sign-in."""
    server.stop()
    server.extra_env = {
        key: value
        for key, value in server.extra_env.items()
        if not key.startswith("CLERK_") and key != "PREP_AUTH_MODE"
    }
    server.start()
    identity["tailscale-user-login"] = SIGNED_IN_LOGIN
    identity["tailscale-user-name"] = SIGNED_IN_NAME


def _cookie(ctx, name: str) -> str | None:
    for entry in ctx.cookies():
        if entry["name"] == name:
            return entry["value"]
    return None


def _server_state(db_path) -> dict:
    """What the server thinks it holds. One deck exists for the whole
    flow, so the query needs no id."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        deck = conn.execute("SELECT user_id, name, display_name FROM decks").fetchone()
        return {
            "deck_owner": deck["user_id"] if deck else None,
            "deck_display": deck["display_name"] if deck else None,
            "questions": conn.execute("SELECT COUNT(*) AS n FROM questions").fetchone()["n"],
            "reviews": conn.execute("SELECT COUNT(*) AS n FROM reviews").fetchone()["n"],
            "anon_users": conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE is_anonymous = 1"
            ).fetchone()["n"],
            "merges": [
                dict(row)
                for row in conn.execute(
                    "SELECT status, anon_user_id, target_user_id FROM account_merges ORDER BY id"
                ).fetchall()
            ],
        }
    finally:
        conn.close()


def _shot(rec: FlowRecorder, page, label: str):
    """Screenshot once the page has stopped moving. Cross-document view
    transitions are on app-wide (static/css/base.css), so a shot taken
    the instant a navigation resolves catches the cross-fade with both
    pages in the frame. A still-running animation after the wait is not
    a reason to lose the evidence, so the wait is bounded."""
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if page.evaluate("() => document.getAnimations().every((a) => a.playState !== 'running')"):
            break
        page.wait_for_timeout(100)
    return rec.shot(page, label)


def _study_one_card(page) -> None:
    """Answer the first due card correctly and take the verdict. Which
    grader applies depends on the card shape, so both the direct
    verdict and the self-grade step in front of it are accepted."""
    prompt = page.locator(".study-prompt").inner_text()
    page.locator(".study-card textarea").fill(ANSWERS[prompt])
    page.get_by_role("button", name="Submit").click()
    page.wait_for_selector("h1.verdict-headline, .offline-selfgrade-blurb")
    if page.locator(".offline-selfgrade-blurb").count():
        page.get_by_role("button", name="I got it right").click()
    page.wait_for_selector("h1.verdict-headline")


def test_anonymous_geography_deck_survives_sign_in(flow_server, flow_ctx):
    """Splash to merged account in one browser: the deck a visitor
    makes before they have an account is the same deck they find after
    signing in, cards and study history included."""
    flow_server.start()  # idempotent; heals a prior test's failure state
    ctx, identity = flow_ctx
    base = flow_server.base_url
    page = ctx.new_page()
    rec = FlowRecorder("anonymous-merge-geography")

    # -- 1. the splash, with no identity of any kind --------------------
    page.goto(base + "/")
    hero = page.locator("[data-instant-start]")
    hero.wait_for()
    assert "What do you want to learn today?" in hero.locator("h1").inner_text()
    # No account chip: the masthead offers sign-in, nothing else.
    assert page.locator(".user-indicator").count() == 0
    assert _cookie(ctx, "prep_anon") is None
    _shot(rec, page, "splash")

    # -- 2. create the deck ---------------------------------------------
    page.locator(".instant-form textarea").fill(TOPIC)
    _shot(rec, page, "topic-entered")
    page.get_by_role("button", name="Generate my deck").click()
    page.wait_for_url("**/deck/**")
    deck_url = page.url
    page.wait_for_selector(".deck-hero-cta")
    assert page.locator(".prelude h1, .deck-hero h1").first.inner_text() == TOPIC
    deck_text = page.locator("body").inner_text()
    for card in GEOGRAPHY_DECK["cards"]:
        assert card["prompt"] in deck_text
    _shot(rec, page, "deck-created")

    # The deck is server-side, under a freshly minted anonymous account
    # the cookie names.
    assert _cookie(ctx, "prep_anon")
    before = _server_state(flow_server.db_path)
    anon_id = before["deck_owner"]
    assert anon_id.startswith("anon:")
    assert before["questions"] == len(GEOGRAPHY_DECK["cards"])

    # -- 3. study the deck ----------------------------------------------
    page.get_by_role("button", name="Begin study session").click()
    page.wait_for_selector(".study-card textarea")
    _shot(rec, page, "study-card")
    _study_one_card(page)
    assert page.locator("h1.verdict-headline").inner_text() == "Right."
    _shot(rec, page, "study-verdict")

    studied = _server_state(flow_server.db_path)
    assert studied["reviews"] == 1, "the anonymous review never reached the server"

    # -- 4. sign in: the merge runs server side on the first request
    #       carrying the identity and the cookie together ---------------
    _sign_in(flow_server, identity)
    page.goto(base + "/")
    page.wait_for_selector(".deck-list .deck-card")
    assert TOPIC in page.locator(".deck-list").inner_text()
    # The owner guard read previous_ids and never prompted, so the
    # device's own copy is not up for a wipe.
    assert page.locator("dialog.offline-owner-dialog").count() == 0
    _shot(rec, page, "signed-in-dashboard")

    page.locator(".user-indicator summary").click()
    page.wait_for_selector(".user-indicator[open] .user-panel")
    assert page.locator(".user-panel .user-login").inner_text() == SIGNED_IN_LOGIN
    _shot(rec, page, "signed-in-account")

    # The deck itself moved, not just its name on a list: the URL the
    # anonymous visitor landed on still serves every card.
    page.goto(deck_url)
    page.wait_for_selector(".deck-hero-cta")
    merged_text = page.locator("body").inner_text()
    for card in GEOGRAPHY_DECK["cards"]:
        assert card["prompt"] in merged_text
    _shot(rec, page, "merged-deck")
    rec.manifest()

    after = _server_state(flow_server.db_path)
    assert after["deck_owner"] == SIGNED_IN_LOGIN, "the anonymous deck did not move"
    assert after["deck_display"] == TOPIC
    assert after["questions"] == before["questions"]
    # Reviews hang off the question, so the study history rides along
    # with it. The account is now studying a deck with a past.
    assert after["reviews"] == studied["reviews"]
    assert after["anon_users"] == 0
    # One navigation issues several authenticated requests, and every
    # one in flight when the merge lands still carries the cookie: the
    # rest find no anonymous row and record the attempt. Exactly one of
    # them moved data.
    statuses = [merge["status"] for merge in after["merges"]]
    assert statuses.count("completed") == 1
    assert set(statuses) <= {"completed", "failed"}
    assert {merge["anon_user_id"] for merge in after["merges"]} == {anon_id}
    assert {merge["target_user_id"] for merge in after["merges"]} == {SIGNED_IN_LOGIN}
    # Nothing left for the cookie to name.
    assert _cookie(ctx, "prep_anon") is None
