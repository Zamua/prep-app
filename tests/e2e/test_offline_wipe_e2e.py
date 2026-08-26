"""End-to-end one-time legacy wipe (docs/ANONYMOUS-ACCOUNTS.md
section 9).

A device may still hold the device-local deck a retired client-side
flow wrote: `meta.guest`, `meta.guest_nudge`, `local_cards` rows and
queued reviews. The wipe removes exactly that, on both boot paths,
BEFORE anything reads those stores.

What each test pins:

- The online path: the wipe runs ahead of `flushOutbox`, so the sync
  transport is never handed a legacy card or review. Reordering the
  two statements posts them into the signed-in account, which is the
  silent adoption this gate exists to prevent.
- The offline path: a device that reaches `/offline` first is wiped
  there, with no online page in between.
- The gate: owner-absent `local_cards` WITHOUT `meta.guest` are a
  signed-in user's offline authoring and must SURVIVE. The blocked
  flush forces the stamping refresh, and the cards flush on the next
  pass.
- The shell's no-owner render, in both directions: it counts those
  surviving rows, and says "Nothing cached" only when the device
  really holds nothing.
- After the stamp, a different sign-in is a mismatch, not an
  absorption: the confirm-then-wipe dialog fires and nothing syncs.

Runs against the LOCAL celld node (offline_server fixture) with
route-injected identity headers. sw.js is blocked: these tests
need repeated authenticated NAVIGATIONS, and a controlling SW
re-issues navigations outside Playwright's routing, dropping the
injected header.
"""

from __future__ import annotations

import pytest

from tests.e2e.celld_node import identity_headers
from tests.e2e.test_offline_study_e2e import (
    _IDB_META_GET_JS,
    _idb_all,
    _module_prefix,
    _wait_for,
)

pytestmark = [pytest.mark.slow, pytest.mark.browser]

LEGACY_DECK_NAME = "Legacy Capitals"
KENYA_PROMPT = "Capital of Kenya?"
GHANA_PROMPT = "Capital of Ghana?"
AUTHORED_PROMPT = "Capital of Mongolia?"


@pytest.fixture()
def wipe_ctx(browser_session, offline_server):
    """Header-injected context with a mutable identity (so one device
    can be visited by two users) and every POST to the sync endpoint
    recorded, so 'nothing synced' is a direct observation."""
    identity = {"login": "wipe-e2e@example.com", "name": "Wipe Tester"}
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
                **identity_headers(identity["login"], identity["name"]),
            }
            route.continue_(headers=headers)
        else:
            route.continue_()

    def _count_sync_posts(route):
        sync_posts.append(route.request.post_data or "")
        route.fallback()  # falls through to the header injection

    # Playwright dispatches the most recently registered matching
    # handler first; sync POSTs fall back into the header injection.
    ctx.route("**/*", _inject_header)
    ctx.route("**/api/offline/sync", _count_sync_posts)
    ctx.route("**/sw.js", lambda route: route.abort())
    try:
        yield ctx, identity, sync_posts
    finally:
        ctx.close()


# The rows the retired flow wrote: the marker, both cards, one queued
# review keyed by card_client_id, the nudge-dismissal record, and one
# server-rejected row (the shape an interrupted hand-off parks).
_SEED_LEGACY_JS = """
async ({prefix, deckName, kenyaPrompt, ghanaPrompt}) => {
  const store = await import(prefix + "offline/store.js");
  const now = new Date().toISOString();
  const mk = (prompt, answer) => ({
    client_id: store.uuid(),
    deck_id: null,
    deck_name: deckName,
    type: "short",
    prompt,
    answer,
    created_at: now,
    local_step: 0,
    local_next_due: null,
  });
  const kenya = mk(kenyaPrompt, "Nairobi");
  const ghana = mk(ghanaPrompt, "Accra");
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
    await store.put("rejects", {
      client_id: store.uuid(),
      kind: "card",
      error: "rejected",
      prompt: ghanaPrompt,
      rejected_at: now,
    });
    await store.put(
      "meta",
      {deck_client_id: store.uuid(), display_name: deckName, topic: "african capitals",
       created_at: now},
      "guest"
    );
    await store.put("meta", {dismissed_at: now}, "guest_nudge");
  });
  return {kenya: kenya.client_id, ghana: ghana.client_id};
}
"""

# One owner-absent authored card in the offline-authoring shape: no
# deck label, no regex, and crucially no legacy marker.
_SEED_AUTHORED_JS = """
async ({prefix, prompt}) => {
  const store = await import(prefix + "offline/store.js");
  await store.withLock(async () => {
    await store.remove("meta", "owner");
    await store.put("local_cards", {
      client_id: store.uuid(),
      deck_id: null,
      prompt,
      answer: "Ulaanbaatar",
      created_at: new Date().toISOString(),
      local_step: 0,
      local_next_due: null,
    });
  });
  return (await store.getAll("local_cards")).length;
}
"""


def _questions_with(server, prompt: str, user: str) -> int:
    """Scoped to one account, which is now one cell: a sibling test's
    flush of the same prompt into another account is not this test's
    leak."""
    return len([q for q in server.rows(user, "questions") if q["prompt"] == prompt])


def _meta_keys_gone(page) -> bool:
    return (
        page.evaluate(_IDB_META_GET_JS, "guest") is None
        and page.evaluate(_IDB_META_GET_JS, "guest_nudge") is None
    )


def test_the_wipe_runs_before_the_flush_on_the_online_path(offline_server, wipe_ctx):
    """The ordering pin. The device is stamped with the signed-in
    owner AND still holds legacy rows (the shape an interrupted
    hand-off leaves), so a flush that ran first would post them
    straight into the account. The wipe goes first, so the transport
    is never given them."""
    ctx, identity, sync_posts = wipe_ctx
    identity["login"] = "wipe-online@example.com"
    identity["name"] = "Wipe Online"
    base = offline_server.base_url
    page = ctx.new_page()
    offline_server.start()  # idempotent; heals a prior test's failure state

    # A normal signed-in device first: the refresh stamps the owner.
    page.goto(base + "/")
    prefix = _module_prefix(page)
    owner = _wait_for(
        lambda: page.evaluate(_IDB_META_GET_JS, "owner"),
        message="owner stamped on the first online load",
    )
    assert owner["user_id"] == identity["login"]
    sync_posts.clear()

    page.evaluate(
        _SEED_LEGACY_JS,
        {
            "prefix": prefix,
            "deckName": LEGACY_DECK_NAME,
            "kenyaPrompt": KENYA_PROMPT,
            "ghanaPrompt": GHANA_PROMPT,
        },
    )
    assert len(_idb_all(page, "local_cards")) == 2
    assert len(_idb_all(page, "outbox_reviews")) == 1
    assert len(_idb_all(page, "rejects")) == 1

    # The next online load boots sync.js init(): wipe, then flush.
    page.goto(base + "/")
    _wait_for(
        lambda: _idb_all(page, "local_cards") == [],
        message="the legacy cards to be wiped",
    )
    assert _idb_all(page, "outbox_reviews") == []
    assert _meta_keys_gone(page)
    assert _idb_all(page, "rejects") == []

    # Nothing was posted, and in particular no legacy card or review.
    assert sync_posts == []
    assert _questions_with(offline_server, KENYA_PROMPT, identity["login"]) == 0
    assert _questions_with(offline_server, GHANA_PROMPT, identity["login"]) == 0
    # The retired confirm is gone from the build, not merely unreached.
    assert page.locator("dialog.offline-adoption-dialog").count() == 0

    # A later load stays clean: the wipe is one-shot, not a loop.
    page.goto(base + "/")
    page.wait_for_selector(".prelude")
    assert sync_posts == []
    assert page.evaluate(_IDB_META_GET_JS, "owner")["user_id"] == identity["login"]


def test_the_wipe_runs_on_the_offline_shell_boot(offline_server, wipe_ctx):
    """A device that reaches /offline first is wiped there: no online
    page in between, and the shell never renders the legacy deck."""
    ctx, identity, sync_posts = wipe_ctx
    identity["login"] = "wipe-shell@example.com"
    identity["name"] = "Wipe Shell"
    base = offline_server.base_url
    page = ctx.new_page()
    offline_server.start()  # idempotent; heals a prior test's failure state

    # The shell runs no sync machinery, so seeding here reaches no
    # online boot path (offline.html loads offline-app.js only).
    page.goto(base + "/offline")
    prefix = _module_prefix(page)
    page.evaluate(
        _SEED_LEGACY_JS,
        {
            "prefix": prefix,
            "deckName": LEGACY_DECK_NAME,
            "kenyaPrompt": KENYA_PROMPT,
            "ghanaPrompt": GHANA_PROMPT,
        },
    )
    assert len(_idb_all(page, "local_cards")) == 2

    page.goto(base + "/offline")
    page.wait_for_selector("[data-offline-root] .prelude")
    assert _idb_all(page, "local_cards") == []
    assert _idb_all(page, "outbox_reviews") == []
    # Needs-attention rows go with them: nothing can retry one, and it
    # would otherwise render forever under the next account's name.
    assert _idb_all(page, "rejects") == []
    assert _meta_keys_gone(page)
    # No owner, nothing cached: the honest empty state, and the deck
    # name never rendered.
    root_text = page.locator("#offline-root").inner_text()
    assert "Nothing cached" in root_text
    assert LEGACY_DECK_NAME not in root_text
    assert sync_posts == []


def test_the_shell_counts_owner_absent_authored_cards(offline_server, wipe_ctx):
    """The marker-ABSENT half of the same boot. Those rows are a
    signed-in user's offline authoring, so the shell names them as
    waiting instead of reporting an empty device to their author."""
    ctx, identity, sync_posts = wipe_ctx
    identity["login"] = "wipe-unstamped@example.com"
    identity["name"] = "Wipe Unstamped"
    base = offline_server.base_url
    page = ctx.new_page()
    offline_server.start()  # idempotent; heals a prior test's failure state

    page.goto(base + "/offline")
    prefix = _module_prefix(page)
    assert page.evaluate(_SEED_AUTHORED_JS, {"prefix": prefix, "prompt": AUTHORED_PROMPT}) == 1

    page.goto(base + "/offline")
    page.wait_for_selector("[data-offline-root] .prelude")
    root_text = page.locator("#offline-root").inner_text()
    assert "1 card is saved on this device and not synced yet" in root_text
    assert "Nothing cached" not in root_text

    # The copy is a claim about rows that have to still be there.
    assert len(_idb_all(page, "local_cards")) == 1
    assert page.evaluate(_IDB_META_GET_JS, "owner") is None
    assert sync_posts == []


def test_owner_absent_authored_cards_survive_and_flush_on_the_next_pass(offline_server, wipe_ctx):
    """The gate's other half. Owner-absent local_cards with no legacy
    marker are offline authoring: the wipe must not touch them, the
    blocked flush must force the stamping refresh, and the next pass
    must flush them."""
    ctx, identity, sync_posts = wipe_ctx
    identity["login"] = "wipe-authored@example.com"
    identity["name"] = "Wipe Authored"
    base = offline_server.base_url
    page = ctx.new_page()
    offline_server.start()  # idempotent; heals a prior test's failure state

    page.goto(base + "/")
    prefix = _module_prefix(page)
    _wait_for(
        lambda: page.evaluate(_IDB_META_GET_JS, "owner"),
        message="owner stamped on the first online load",
    )
    # Drop the stamp and author a card: a device that authored before
    # its first successful snapshot refresh.
    assert page.evaluate(_SEED_AUTHORED_JS, {"prefix": prefix, "prompt": AUTHORED_PROMPT}) == 1
    assert page.evaluate(_IDB_META_GET_JS, "owner") is None
    sync_posts.clear()

    # Pass one: the flush is blocked (no stamp to flush under), the
    # forced refresh stamps, and the card is still here.
    page.goto(base + "/")
    owner = _wait_for(
        lambda: page.evaluate(_IDB_META_GET_JS, "owner"),
        message="the blocked flush to force a stamping refresh",
    )
    assert owner["user_id"] == identity["login"]
    assert len(_idb_all(page, "local_cards")) == 1
    assert sync_posts == [], "the unstamped device must not flush"

    # Pass two: the card flushes into the account it belongs to.
    page.goto(base + "/")
    _wait_for(
        lambda: _idb_all(page, "local_cards") == [],
        message="the authored card to flush on the next pass",
    )
    assert len(sync_posts) >= 1
    assert any(AUTHORED_PROMPT in body for body in sync_posts)
    assert _questions_with(offline_server, AUTHORED_PROMPT, identity["login"]) == 1
    assert _idb_all(page, "rejects") == []


def test_a_different_sign_in_after_the_stamp_is_a_mismatch_not_an_absorption(
    offline_server, wipe_ctx
):
    """Absent is transient and self-closing; once the stamp lands, a
    different account is a stranger. The dialog fires and the cards
    stay put instead of flushing into the new account."""
    ctx, identity, sync_posts = wipe_ctx
    identity["login"] = "wipe-first@example.com"
    identity["name"] = "Wipe First"
    base = offline_server.base_url
    page = ctx.new_page()
    offline_server.start()  # idempotent; heals a prior test's failure state

    page.goto(base + "/")
    prefix = _module_prefix(page)
    _wait_for(
        lambda: page.evaluate(_IDB_META_GET_JS, "owner"),
        message="owner stamped on the first online load",
    )
    assert page.evaluate(_SEED_AUTHORED_JS, {"prefix": prefix, "prompt": AUTHORED_PROMPT}) == 1
    sync_posts.clear()

    # The stamping pass, as the first user.
    page.goto(base + "/")
    owner = _wait_for(
        lambda: page.evaluate(_IDB_META_GET_JS, "owner"),
        message="the blocked flush to force a stamping refresh",
    )
    assert owner["user_id"] == "wipe-first@example.com"
    assert sync_posts == []

    # A different person signs in on the same device.
    identity["login"] = "wipe-second@example.com"
    identity["name"] = "Wipe Second"
    page.goto(base + "/")
    dialog = page.locator("dialog.offline-owner-dialog[open]")
    dialog.wait_for()
    assert "1 unsynced new card" in dialog.inner_text()

    assert sync_posts == [], "the mismatched sign-in must not absorb the card"
    assert len(_idb_all(page, "local_cards")) == 1
    assert page.evaluate(_IDB_META_GET_JS, "owner")["user_id"] == "wipe-first@example.com"
    for login in ("wipe-first@example.com", "wipe-second@example.com"):
        assert _questions_with(offline_server, AUTHORED_PROMPT, login) == 0
