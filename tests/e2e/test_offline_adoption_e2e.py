"""End-to-end guest adoption gate (docs/INSTANT-START.md sections 2
and 3.4, M3 scope).

Guest data is seeded directly into IndexedDB (owner absent: the shape
instant-start generation writes), then the device loads the app as a
signed-in user. The suite pins the state-2 contract:

- The adoption confirm appears BEFORE any sync effect: zero POSTs to
  the sync endpoint, no meta.owner stamp, server rows untouched.
- Accept flushes the guest deck through the normal machinery (cards
  first, carrying deck_name + answer_regex on the wire), stamps the
  owner, and deletes meta.guest. Accept COMPLETING is itself the pin
  on the adoptionApproved latch: without the disarm the gate refuses
  Accept's own flush and the owner never stamps.
- Discard wipes the guest rows and proceeds as a fresh device (seed
  refresh stamps the owner) without a single sync POST.
- Dismiss decides nothing: guest data intact, gate still holding for
  direct flush/refresh calls, re-prompt on the next load.

Runs against the LOCAL tailscale-mode server (offline_server fixture)
with route-injected identity headers. sw.js is blocked in this suite:
the tests need repeated authenticated NAVIGATIONS, and a controlling
SW re-issues navigations itself, outside Playwright's routing, which
would drop the injected header (see the fixture-block notes in
conftest.py). The adoption gate has no service-worker dependency.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from tests.e2e.test_offline_m5_e2e import _server_counts
from tests.e2e.test_offline_study_e2e import (
    _IDB_META_GET_JS,
    _idb_all,
    _module_prefix,
    _wait_for,
)

pytestmark = [pytest.mark.slow, pytest.mark.browser]

GUEST_DECK_NAME = "Guest Capitals"
KENYA_PROMPT = "Capital of Kenya?"
GHANA_PROMPT = "Capital of Ghana?"


@pytest.fixture()
def adoption_ctx(browser_session, offline_server):
    """Header-injected context for the adoption suite. The identity
    dict is mutable so each test signs in as its own user (the shared
    session db then needs no cross-test cleanup). Every POST to the
    sync endpoint is recorded so 'no sync happened' is a direct
    observation. sw.js is blocked (see the module docstring)."""
    identity = {"login": "adoption-e2e@example.com", "name": "Adoption Tester"}
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

    base = offline_server.base_url

    def _inject_header(route, request):
        if request.url.startswith(base):
            headers = {
                **request.headers,
                "tailscale-user-login": identity["login"],
                "tailscale-user-name": identity["name"],
            }
            route.continue_(headers=headers)
        else:
            route.continue_()

    def _count_sync_posts(route):
        sync_posts.append(route.request.post_data or "")
        route.fallback()  # falls through to the header injection

    def _block_sw(route):
        route.abort()

    # Playwright dispatches the most recently registered matching
    # handler first; sync POSTs fall back into the header injection.
    ctx.route("**/*", _inject_header)
    ctx.route("**/api/offline/sync", _count_sync_posts)
    ctx.route("**/sw.js", _block_sw)
    try:
        yield ctx, identity, sync_posts
    finally:
        ctx.close()


# Seed the exact rows instant-start generation writes on an
# owner-absent device (docs/INSTANT-START.md section 3.3): meta.guest,
# guest local_cards rows (deck_name + type "short" + answer_regex),
# and one queued review keyed by card_client_id.
_SEED_GUEST_JS = """
async ({prefix, deckName, kenyaPrompt, ghanaPrompt}) => {
  const store = await import(prefix + "offline/store.js");
  const now = new Date().toISOString();
  const mk = (prompt, answer, regex) => ({
    client_id: store.uuid(),
    deck_id: null,
    deck_name: deckName,
    type: "short",
    prompt,
    answer,
    answer_regex: regex,
    created_at: now,
    local_step: 0,
    local_next_due: null,
  });
  const kenya = mk(kenyaPrompt, "Nairobi", "nairobi");
  const ghana = mk(ghanaPrompt, "Accra", null);
  await store.withLock(async () => {
    await store.put("local_cards", kenya);
    await store.put("local_cards", ghana);
    await store.put("outbox_reviews", {
      client_id: store.uuid(),
      card_client_id: kenya.client_id,
      verdict: "right",
      user_answer: "Nairobi",
      graded_by: "auto",
      reviewed_at: now,
    });
    await store.put(
      "meta",
      {
        deck_client_id: store.uuid(),
        display_name: deckName,
        topic: "african capitals",
        created_at: now,
      },
      "guest"
    );
  });
  return {kenya: kenya.client_id, ghana: ghana.client_id};
}
"""

_FETCH_SNAPSHOT_JS = """
async () => {
  const r = await fetch("/api/offline/snapshot", {
    credentials: "same-origin",
    headers: {accept: "application/json"},
  });
  return r.json();
}
"""

_GATE_HOLDS_JS = """
async ({prefix}) => {
  const sync = await import(prefix + "offline/sync.js");
  const pending = await sync.guestAdoptionPending();
  const flush = await sync.flushOutbox();
  const refresh = await sync.refreshSnapshot({force: true});
  return {
    pending,
    flushPending: Boolean(flush.adoption_pending),
    refreshPending: Boolean(refresh.adoption_pending),
  };
}
"""


def _seed_guest_device(page, base: str) -> str:
    """Seed guest data on the un-auth-gated /offline shell (no sync
    machinery runs there for an owner-less device) and return the
    versioned module prefix."""
    page.goto(base + "/offline")
    prefix = _module_prefix(page)
    page.evaluate(
        _SEED_GUEST_JS,
        {
            "prefix": prefix,
            "deckName": GUEST_DECK_NAME,
            "kenyaPrompt": KENYA_PROMPT,
            "ghanaPrompt": GHANA_PROMPT,
        },
    )
    assert page.evaluate(_IDB_META_GET_JS, "owner") is None
    assert page.evaluate(_IDB_META_GET_JS, "guest")["display_name"] == GUEST_DECK_NAME
    return prefix


def _open_authenticated(page, base: str, sync_posts: list[str]) -> None:
    """Load the app signed in and assert the confirm appears BEFORE
    any sync effect: no POST, no owner stamp."""
    page.goto(base + "/")
    dialog = page.locator("dialog.offline-adoption-dialog[open]")
    dialog.wait_for()
    text = dialog.inner_text()
    assert "Add your deck to this account?" in text
    assert f"{GUEST_DECK_NAME}: 2 cards, 1 review from before you signed in." in text
    assert sync_posts == []
    assert page.evaluate(_IDB_META_GET_JS, "owner") is None


def test_adoption_accept_flushes_the_guest_deck_into_the_account(offline_server, adoption_ctx):
    """Accept: the latch lets Accept's own flush + forced refresh
    through the gate; the deck, cards, regex, and replayed review land
    in the signed-in account; meta.owner stamps; meta.guest dies."""
    ctx, identity, sync_posts = adoption_ctx
    identity["login"] = "adoption-accept@example.com"
    identity["name"] = "Adoption Accept"
    base = offline_server.base_url
    page = ctx.new_page()
    offline_server.start()  # idempotent; heals a prior test's failure state

    _seed_guest_device(page, base)
    baseline = _server_counts(offline_server.db_path)
    _open_authenticated(page, base, sync_posts)
    assert _server_counts(offline_server.db_path) == baseline

    page.get_by_role("button", name="Add to my account").click()
    page.locator("dialog.offline-adoption-dialog").wait_for(state="detached")

    # Owner stamped, guest record gone, guest queues drained. This
    # completing at all pins the adoptionApproved latch: without it
    # the gate refuses Accept's own flush and the owner never stamps.
    owner = _wait_for(
        lambda: page.evaluate(_IDB_META_GET_JS, "owner"),
        message="owner stamped after accept",
    )
    assert owner["user_id"] == identity["login"]
    assert page.evaluate(_IDB_META_GET_JS, "guest") is None
    assert _idb_all(page, "local_cards") == []
    assert _idb_all(page, "outbox_reviews") == []
    assert _idb_all(page, "rejects") == []

    # Cards-first wire order, with the new fields on the card chunk
    # and the null regex staying off the wire.
    bodies = [json.loads(b) for b in sync_posts]
    card_chunks = [i for i, b in enumerate(bodies) if b.get("new_cards")]
    review_chunks = [i for i, b in enumerate(bodies) if b.get("reviews")]
    assert card_chunks and review_chunks
    assert card_chunks[0] < review_chunks[0]
    wire_cards = {c["prompt"]: c for c in bodies[card_chunks[0]]["new_cards"]}
    assert wire_cards[KENYA_PROMPT]["deck_name"] == GUEST_DECK_NAME
    assert wire_cards[KENYA_PROMPT]["answer_regex"] == "nairobi"
    assert wire_cards[GHANA_PROMPT]["deck_name"] == GUEST_DECK_NAME
    assert "answer_regex" not in wire_cards[GHANA_PROMPT]

    # Server truth: one SRS deck named by deck_name, both cards in it,
    # regex stored on the question row, the anonymous review replayed
    # through the real scheduler (step 0 -> 1 on right).
    conn = sqlite3.connect(offline_server.db_path)
    try:
        decks = conn.execute(
            "SELECT id, COALESCE(deck_type, 'srs'), display_name FROM decks WHERE user_id = ?",
            (identity["login"],),
        ).fetchall()
        assert len(decks) == 1
        deck_id, deck_type, display_name = decks[0]
        assert deck_type == "srs"
        assert display_name == GUEST_DECK_NAME
        questions = conn.execute(
            "SELECT id, prompt, answer_regex, deck_id FROM questions WHERE user_id = ?",
            (identity["login"],),
        ).fetchall()
        by_prompt = {q[1]: q for q in questions}
        assert set(by_prompt) == {KENYA_PROMPT, GHANA_PROMPT}
        assert by_prompt[KENYA_PROMPT][2] == "nairobi"
        assert by_prompt[GHANA_PROMPT][2] is None
        assert all(q[3] == deck_id for q in questions)
        kenya_qid = by_prompt[KENYA_PROMPT][0]
        reviews = conn.execute(
            "SELECT result FROM reviews WHERE question_id = ?", (kenya_qid,)
        ).fetchall()
        assert [r[0] for r in reviews] == ["right"]
        step = conn.execute(
            "SELECT step FROM cards WHERE question_id = ?", (kenya_qid,)
        ).fetchone()[0]
        assert step == 1
    finally:
        conn.close()

    # And the same state is visible through the API the device syncs
    # with: the adopted deck + cards come back in the snapshot.
    snapshot = page.evaluate(_FETCH_SNAPSHOT_JS)
    assert GUEST_DECK_NAME in [d["display_name"] for d in snapshot["decks"]]
    snap_cards = {c["prompt"]: c for c in snapshot["cards"]}
    assert set(snap_cards) == {KENYA_PROMPT, GHANA_PROMPT}
    assert snap_cards[KENYA_PROMPT]["answer_regex"] == "nairobi"
    assert page.locator(".offline-toast").inner_text() == "Deck added to your account."


def test_adoption_discard_wipes_guest_data_and_seeds_normally(offline_server, adoption_ctx):
    """Discard: guest rows cleared, meta.guest gone, then the normal
    state-1 seed refresh stamps the owner. The server never sees a
    single sync POST or any guest row."""
    ctx, identity, sync_posts = adoption_ctx
    identity["login"] = "adoption-discard@example.com"
    identity["name"] = "Adoption Discard"
    base = offline_server.base_url
    page = ctx.new_page()
    offline_server.start()  # idempotent; heals a prior test's failure state

    _seed_guest_device(page, base)
    baseline = _server_counts(offline_server.db_path)
    _open_authenticated(page, base, sync_posts)

    page.get_by_role("button", name="Discard it").click()
    page.locator("dialog.offline-adoption-dialog").wait_for(state="detached")

    owner = _wait_for(
        lambda: page.evaluate(_IDB_META_GET_JS, "owner"),
        message="owner seeded after discard",
    )
    assert owner["user_id"] == identity["login"]
    assert page.evaluate(_IDB_META_GET_JS, "guest") is None
    assert _idb_all(page, "local_cards") == []
    assert _idb_all(page, "outbox_reviews") == []
    assert _idb_all(page, "rejects") == []

    # Explicit data loss stays local: nothing was ever POSTed and no
    # guest row exists anywhere server-side.
    assert sync_posts == []
    assert _server_counts(offline_server.db_path) == baseline
    conn = sqlite3.connect(offline_server.db_path)
    try:
        leaked = conn.execute(
            "SELECT COUNT(*) FROM questions WHERE user_id = ?", (identity["login"],)
        ).fetchone()[0]
    finally:
        conn.close()
    assert leaked == 0


def test_adoption_dismiss_decides_nothing_and_reprompts(offline_server, adoption_ctx):
    """Dismiss (Esc/backdrop): no flush, no owner stamp, guest data
    intact, the gate still refuses direct flush/refresh calls, and the
    next authenticated load re-prompts."""
    ctx, identity, sync_posts = adoption_ctx
    identity["login"] = "adoption-dismiss@example.com"
    identity["name"] = "Adoption Dismiss"
    base = offline_server.base_url
    page = ctx.new_page()
    offline_server.start()  # idempotent; heals a prior test's failure state

    prefix = _seed_guest_device(page, base)
    baseline = _server_counts(offline_server.db_path)
    _open_authenticated(page, base, sync_posts)

    page.keyboard.press("Escape")
    page.locator("dialog.offline-adoption-dialog").wait_for(state="detached")

    # Undecided is not consent: everything exactly as seeded.
    assert page.evaluate(_IDB_META_GET_JS, "owner") is None
    assert page.evaluate(_IDB_META_GET_JS, "guest")["display_name"] == GUEST_DECK_NAME
    assert len(_idb_all(page, "local_cards")) == 2
    assert len(_idb_all(page, "outbox_reviews")) == 1
    assert sync_posts == []
    assert _server_counts(offline_server.db_path) == baseline

    # The gate holds for direct calls too: both entry points refuse
    # with adoption_pending and still nothing reaches the server.
    gate = page.evaluate(_GATE_HOLDS_JS, {"prefix": prefix})
    assert gate == {"pending": True, "flushPending": True, "refreshPending": True}
    assert sync_posts == []
    assert page.evaluate(_IDB_META_GET_JS, "owner") is None

    # Next authenticated load re-prompts.
    page.goto(base + "/")
    page.locator("dialog.offline-adoption-dialog[open]").wait_for()
    assert sync_posts == []
