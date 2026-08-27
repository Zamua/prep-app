"""Leaving a browser that holds this account's cards.

Signing out leaves the snapshot in place by design, so the device
keeps rendering the previous account's decks to whoever uses it next.
Nothing said so, and nothing offered removal at the moment the user
was leaving. What each test pins:

- Sign-out on a device holding a snapshot opens the choice, and each
  of its three exits does what its label says. The proof is the
  IndexedDB state after each one, not the navigation.
- Sign-out on a device holding nothing navigates with no dialog: the
  warning is data-driven, never unconditional friction.
- The removal flushes BEFORE it clears, asserted as an order (the
  sync POST, then the stores emptying) and not merely as an end
  state. The queued review and the offline-authored card are read
  back out of the server's own database.
- An unreachable server does not cost the user that work: the wipe
  stops, names what is unsaved, and destroys it only on a second
  explicit choice. Cancel there leaves everything in place, including
  the session, and the same dialog carries its own sign-out exit so
  answering the data question never answers the session question.
- Esc cannot resolve a removal that is mid-flight.
- A row the server permanently refused is named before a wipe can
  destroy it: no flush will ever save it, so this is the only warning
  it gets.
- A row written after a wipe is dropped rather than flushed into
  whichever account signs in next.
- The landing page's status line says the cards are on this browser
  and carries the removal inline.

The sign-out suite runs against a LOCAL celld node started with a
sign-out URL configured (`PREP_PARITY_SIGN_OUT_URL`), so its provider
identifies every request AND the masthead renders the real row with
its real hook (the row follows the provider, pinned in
tests/web/test_signout_choice.py). The landing test needs the opposite
-- a visitor the server cannot identify -- so it uses the shared
offline node with no identity injected.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from tests.e2e.celld_node import LocalCelldNode
from tests.e2e.conftest import inject_identity, new_iphone_context
from tests.e2e.test_offline_study_e2e import _idb_all, _module_prefix, _wait_for

pytestmark = [pytest.mark.slow, pytest.mark.browser]

SNAPSHOT_FLAG = "prep:offline_snapshot"

# Who the node's provider resolves for this suite's contexts.
FAKE_LOGIN = "device-wipe@example.com"
FAKE_NAME = "Device Tester"

DECK_LABEL = "Device Capitals"
AUTHORED_PROMPT = "Capital of Iceland?"

# The landing suite seeds its own device rather than the server's, so
# it never depends on what a sibling suite left in the shared db.
LANDING_DECK_LABEL = "Saved Capitals"
LANDING_DECK_ID = 71


# ---- servers -----------------------------------------------------------


@pytest.fixture(scope="session")
def wipe_server(celld_build):
    """A local node whose provider both identifies every request and has a
    sign-out URL, which is what makes the masthead render the row under
    test. The URL is the parity interstitial the entry worker already
    serves."""
    node = LocalCelldNode("wipe", vars={"PREP_PARITY_SIGN_OUT_URL": PROVIDER_SIGN_OUT_PATH})
    node.start()
    try:
        node.seed = node.seed_profile(FAKE_LOGIN, "device_wipe")
        yield node
    finally:
        node.stop()


# Records that a modal was opened, in storage that outlives the page.
# "No dialog appeared" is otherwise unobservable after the navigation
# it was supposed to precede; the tests that DO open one assert this
# key is set, which is what stops the negative passing vacuously.
DIALOG_KEY = "test:dialog-opened"
ORDER_KEY = "test:order"

_DIALOG_PROBE_JS = f"""
(() => {{
  const open = HTMLDialogElement.prototype.showModal;
  HTMLDialogElement.prototype.showModal = function () {{
    try {{ localStorage.setItem({DIALOG_KEY!r}, this.className || "dialog"); }} catch (e) {{}}
    return open.apply(this, arguments);
  }};
}})();
"""


def _new_ctx(browser_session):
    """Service workers are BLOCKED, and every route in this file is a
    regex. Both are load-bearing: once prep's service worker claims the
    page it re-issues navigations itself, outside Playwright's routing,
    so the sign-out navigation this suite has to observe would be
    invisible."""
    ctx = new_iphone_context(browser_session, service_workers="block")
    ctx.add_init_script(_DIALOG_PROBE_JS)
    return ctx


@pytest.fixture()
def wipe_page(browser_session, wipe_server):
    ctx = _new_ctx(browser_session)
    inject_identity(ctx, wipe_server.base_url, FAKE_LOGIN, FAKE_NAME)
    try:
        yield ctx.new_page()
    finally:
        ctx.close()


@pytest.fixture()
def landing_page(browser_session, offline_server):
    """No identity of any kind, so every load of `/` is the landing."""
    ctx = _new_ctx(browser_session)
    try:
        yield ctx.new_page()
    finally:
        ctx.close()


# ---- page helpers ------------------------------------------------------


# Queue one review of a real snapshot card and one offline-authored
# card: the two stores wipeAll clears and no caller used to flush.
_SEED_OUTBOX_JS = """
async ({prefix, questionId, prompt}) => {
  const store = await import(prefix + "offline/store.js");
  const now = new Date().toISOString();
  await store.withLock(async () => {
    await store.put("outbox_reviews", {
      client_id: store.uuid(),
      question_id: questionId,
      verdict: "right",
      user_answer: "Lima",
      graded_by: "auto",
      reviewed_at: now,
    });
    await store.put("local_cards", {
      client_id: store.uuid(),
      deck_id: null,
      prompt,
      answer: "Reykjavik",
      created_at: now,
      local_step: 0,
      local_next_due: null,
    });
  });
  return [
    (await store.getAll("outbox_reviews")).length,
    (await store.getAll("local_cards")).length,
  ];
}
"""

# Two observations on one timeline. "flush" is recorded when the sync
# transport is handed the queued rows; "wiped" when the decks store
# (which only a wipe clears) goes empty. Consecutive flush chunks
# collapse to one entry. Written to localStorage on every push: the
# wipe is followed by a navigation, and a log on `window` would go
# with the document that recorded it.
_ORDER_JS = """
async ({prefix, key}) => {
  const store = await import(prefix + "offline/store.js");
  const order = [];
  let seeded = false;
  const push = (name) => {
    if (order[order.length - 1] === name) return;
    order.push(name);
    localStorage.setItem(key, JSON.stringify(order));
  };
  const inner = window.fetch;
  window.fetch = function (input) {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    if (url.indexOf("/api/offline/sync") !== -1) push("flush");
    return inner.apply(this, arguments);
  };
  const tick = async () => {
    try {
      const decks = await store.getAll("decks");
      if (decks.length) seeded = true;
      else if (seeded) push("wiped");
    } catch (e) {
      // a storage blip is not an observation
    }
    setTimeout(tick, 20);
  };
  tick();
}
"""

# A row the server refused for good. It is not queued anywhere, so no
# flush will ever move it and the device is its only copy.
_SEED_REJECT_JS = """
async ({prefix}) => {
  const store = await import(prefix + "offline/store.js");
  await store.put("rejects", {
    client_id: store.uuid(),
    kind: "card",
    prompt: "Capital of Malta?",
    answer: "Valletta",
    error: "rejected",
    rejected_at: new Date().toISOString(),
  });
  return (await store.getAll("rejects")).length;
}
"""

# Wipe, then write the way a second tab still on the study screen
# would, then flush twice: the first pass is the one that stamps an
# owner on an unstamped device, and the second is the one that would
# send the row under that owner.
_POST_WIPE_JS = """
async ({prefix, prompt}) => {
  const store = await import(prefix + "offline/store.js");
  const sync = await import(prefix + "offline/sync.js");
  await store.withLock(() => store.wipeAll());
  await store.put("local_cards", {
    client_id: store.uuid(),
    deck_id: null,
    prompt,
    answer: "Nobody's answer",
    created_at: new Date().toISOString(),
  });
  const before = (await store.getAll("local_cards")).length;
  await sync.flushOutbox();
  await sync.flushOutbox();
  return {
    before,
    after: (await store.getAll("local_cards")).length,
    owner: await store.metaGet("owner"),
  };
}
"""

_WIPE_THEN_RESYNC_JS = """
async ({prefix, prompt}) => {
  const store = await import(prefix + "offline/store.js");
  const sync = await import(prefix + "offline/sync.js");
  await store.withLock(() => store.wipeAll());
  const markerAfterWipe = await store.metaGet("wiped");
  await sync.refreshSnapshot({force: true});
  const markerAfterRefresh = await store.metaGet("wiped");
  await store.put("local_cards", {
    client_id: store.uuid(),
    deck_id: null,
    prompt,
    answer: "Kept",
    created_at: new Date().toISOString(),
  });
  await sync.flushOutbox();
  return {
    markerAfterWipe,
    markerAfterRefresh,
    owner: await store.metaGet("owner"),
    after: (await store.getAll("local_cards")).length,
  };
}
"""

_SEED_LANDING_JS = """
async ({prefix, decks, cards, owner}) => {
  const store = await import(prefix + "offline/store.js");
  await store.bulkReplace("decks", decks);
  await store.bulkReplace("cards", cards);
  await store.metaPut("owner", owner);
  store.markSnapshotHeld();
}
"""


def _flag(page):
    return page.evaluate(f"() => localStorage.getItem({SNAPSHOT_FLAG!r})")


def _prime(page, base: str) -> str:
    """Load the dashboard and wait for the snapshot to land, so the
    device under test genuinely holds cards. Counts are lower bounds:
    the server is session-scoped, and a flushed card with no deck
    files into an inbox deck the next test's snapshot then carries."""
    page.goto(base + "/")
    _wait_for(
        lambda: len(_idb_all(page, "decks")) >= 1,
        message="the snapshot deck in IndexedDB",
    )
    _wait_for(lambda: len(_idb_all(page, "cards")) >= 2, message="the snapshot cards")
    assert _flag(page) == "1"
    return _module_prefix(page)


SNAPSHOT_URL = re.compile(r"/api/offline/snapshot$")
SYNC_URL = re.compile(r"/api/offline/sync$")
# Anchored on the whole URL: the provider's own page ends in the same
# segment, and a bare suffix match would swallow the redirect target too.
SIGN_OUT_URL = re.compile(r"^[a-z]+://[^/]+/sign-out$")
PROVIDER_SIGN_OUT_PATH = "/_parity/sign-out"
PROVIDER_SIGN_OUT_URL = re.compile(r"/_parity/sign-out$")


# What the sign-out navigation is answered with in the tests that
# have to read the device afterwards. Same origin, and it runs none of
# the app.
_STUB_PAGE = "<!doctype html><title>signed out</title><p>signed out</p>"


def _freeze_snapshot(page):
    """Stop the device being re-seeded. The fake provider resolves the
    same user after a sign-out, so the page landed on would refresh
    the snapshot straight back -- and "the cards stayed" would be true
    of a device that had in fact been wiped."""
    page.route(SNAPSHOT_URL, lambda route: route.abort())


def _watch_sign_out(page) -> list[str]:
    """Record the sign-out navigation and answer it with a stub page.

    Two reasons not to let it reach the real route: the page it lands
    on runs the app again and would refresh the snapshot straight back
    onto the device under test, and aborting instead commits a browser
    error page, which is a different origin and can read neither
    IndexedDB nor the logs below. The URL is anchored to its end; the
    provider's own sign-out page lives one segment deeper and is not
    reached from here."""
    requested: list[str] = []

    def _handler(route):
        requested.append(route.request.url)
        route.fulfill(status=200, content_type="text/html", body=_STUB_PAGE)

    page.route(SIGN_OUT_URL, _handler)
    return requested


def _settle(page, ms: int = 500):
    """Advance Playwright's event loop. Route handlers only run while
    the test is inside a Playwright call, so a plain sleep would let a
    navigation go unrecorded and read as "nothing happened"."""
    page.wait_for_timeout(ms)


def _storage(page, key: str):
    return page.evaluate("(k) => localStorage.getItem(k)", key)


def _click_sign_out(page):
    """Open the chip panel if it is closed, then take the sign-out
    link. Idempotent: a second call in the same test must not toggle
    the panel back shut."""
    panel = page.locator("details.user-indicator")
    if panel.evaluate("node => !node.open"):
        panel.locator("summary").click()
    link = page.get_by_role("link", name="Sign out")
    link.wait_for(state="visible")
    link.click()


def _choices(dialog) -> list[str]:
    return dialog.locator(".offline-choice-actions button").all_inner_texts()


def _questions_with(server, prompt: str) -> int:
    return len([q for q in server.rows(FAKE_LOGIN, "questions") if q["prompt"] == prompt])


def _reviews_of(server, qid: int) -> int:
    return len([r for r in server.rows(FAKE_LOGIN, "reviews") if r["question_id"] == qid])


# ---- the sign-out choice ------------------------------------------------


def test_signing_out_with_a_snapshot_offers_three_ways_out_and_cancel_stays(wipe_server, wipe_page):
    wipe_server.start()  # idempotent; heals a prior test's failure state
    page = wipe_page
    base = wipe_server.base_url
    _prime(page, base)
    requested = _watch_sign_out(page)

    _click_sign_out(page)
    dialog = page.locator("dialog.offline-signout-dialog[open]")
    dialog.wait_for()

    text = dialog.inner_text()
    assert "stay here after you sign out" in text
    assert "Anyone who uses this browser can open them." in text
    # The removal must not read as deleting the account's decks.
    assert "Removing them clears this browser only. Your account keeps your decks." in text
    assert _choices(dialog) == [
        "Sign out, keep the cards",
        "Sign out and remove them",
        "Cancel",
    ]

    dialog.get_by_role("button", name="Cancel").click()
    dialog.wait_for(state="detached")

    # Cancel is all three: no sign-out, no wipe, no page change.
    _settle(page)
    assert requested == []
    assert page.url.rstrip("/") == base
    assert len(_idb_all(page, "decks")) >= 1
    assert _flag(page) == "1"
    # The probe the no-dialog test reads as a negative can see this one.
    assert _storage(page, DIALOG_KEY) == "offline-choice-dialog offline-signout-dialog"


def test_sign_out_and_keep_signs_out_and_leaves_the_cards(wipe_server, wipe_page):
    wipe_server.start()
    page = wipe_page
    base = wipe_server.base_url
    _prime(page, base)
    decks = len(_idb_all(page, "decks"))
    cards = len(_idb_all(page, "cards"))
    _freeze_snapshot(page)

    _click_sign_out(page)
    dialog = page.locator("dialog.offline-signout-dialog[open]")
    dialog.wait_for()
    dialog.get_by_role("button", name="Sign out, keep the cards").click()

    # The provider's sign-out URL, reached through the app's route.
    page.wait_for_url(PROVIDER_SIGN_OUT_URL)
    # Same origin, so the device is readable from where it landed.
    assert len(_idb_all(page, "decks")) == decks
    assert len(_idb_all(page, "cards")) == cards
    assert _flag(page) == "1"


def test_sign_out_and_remove_flushes_before_it_clears_the_stores(wipe_server, wipe_page):
    """The order pin, and the bug this whole path exists for: wipeAll
    clears outbox_reviews and local_cards, so a wipe that ran first
    would destroy the queued review and the offline-authored card
    without either ever reaching the server."""
    wipe_server.start()
    page = wipe_page
    base = wipe_server.base_url
    prefix = _prime(page, base)
    qid = wipe_server.seed["qids"][0]

    # Seeded after the snapshot lands, so the page-load flush has
    # already run and this queue is the one the removal must carry.
    assert page.evaluate(
        _SEED_OUTBOX_JS, {"prefix": prefix, "questionId": qid, "prompt": AUTHORED_PROMPT}
    ) == [1, 1]
    page.evaluate(_ORDER_JS, {"prefix": prefix, "key": ORDER_KEY})
    requested = _watch_sign_out(page)

    _click_sign_out(page)
    dialog = page.locator("dialog.offline-signout-dialog[open]")
    dialog.wait_for()
    # The line naming what is unsaved belongs to the second dialog;
    # this path is expected to save that work, not warn about it.
    dialog.get_by_role("button", name="Sign out and remove them").click()

    # The wipe is followed by the sign-out navigation, so the device is
    # read from where that lands.
    page.wait_for_url(SIGN_OUT_URL)
    assert requested[0].endswith("/sign-out")

    # The order, recorded on one timeline inside the page.
    assert page.evaluate("(k) => JSON.parse(localStorage.getItem(k))", ORDER_KEY) == [
        "flush",
        "wiped",
    ]
    # And the consequence of that order: the work is on the server.
    assert _reviews_of(wipe_server, qid)
    assert _questions_with(wipe_server, AUTHORED_PROMPT)

    # Then the device: nothing left, and nothing claiming otherwise.
    assert _idb_all(page, "decks") == []
    assert _idb_all(page, "cards") == []
    assert _idb_all(page, "outbox_reviews") == []
    assert _idb_all(page, "local_cards") == []
    assert _flag(page) is None
    # The saving path, so the second dialog never had a reason to open.
    assert _storage(page, DIALOG_KEY) == "offline-choice-dialog offline-signout-dialog"


def test_signing_out_with_no_snapshot_shows_no_dialog(wipe_server, wipe_page):
    """A device with nothing on it has nothing to warn about."""
    wipe_server.start()
    page = wipe_page
    base = wipe_server.base_url
    # Blocked from the first load: the account HAS decks, so without
    # this the background refresh would seed the device under test.
    page.route(SNAPSHOT_URL, lambda route: route.abort())
    requested = _watch_sign_out(page)

    page.goto(base + "/")
    page.locator("details.user-indicator").wait_for()
    assert _idb_all(page, "decks") == []
    assert _flag(page) is None

    _click_sign_out(page)

    page.wait_for_url(SIGN_OUT_URL)
    assert requested[0].endswith("/sign-out")
    # Nothing was opened on the way out. The same probe reads
    # non-empty in every test above, so this is a result.
    assert _storage(page, DIALOG_KEY) is None


def test_an_unreachable_server_costs_the_user_nothing_without_a_second_choice(
    wipe_server, wipe_page
):
    wipe_server.start()
    page = wipe_page
    base = wipe_server.base_url
    prefix = _prime(page, base)
    qid = wipe_server.seed["qids"][1]

    assert page.evaluate(
        _SEED_OUTBOX_JS, {"prefix": prefix, "questionId": qid, "prompt": AUTHORED_PROMPT}
    ) == [1, 1]
    # The flush cannot land; everything else about the page is normal.
    page.route(SYNC_URL, lambda route: route.abort())
    requested = _watch_sign_out(page)

    _click_sign_out(page)
    first = page.locator("dialog.offline-signout-dialog[open]")
    first.wait_for()
    first.get_by_role("button", name="Sign out and remove them").click()

    warning = page.locator("dialog.offline-unsynced-dialog[open]")
    warning.wait_for()
    text = warning.inner_text()
    assert "1 review and 1 new card on this browser have not reached your account yet." in text
    assert "Removing the cards deletes it for good." in text
    # The data question carries its own sign-out exit: cancelling a
    # warning about data must not silently cancel the sign-out too.
    assert _choices(warning) == ["Sign out, keep the cards", "Remove anyway", "Cancel"]

    warning.get_by_role("button", name="Cancel").click()
    warning.wait_for(state="detached")

    # Cancel destroyed nothing and signed nobody out.
    _settle(page)
    assert len(_idb_all(page, "outbox_reviews")) == 1
    assert len(_idb_all(page, "local_cards")) == 1
    assert len(_idb_all(page, "decks")) >= 1
    assert requested == []

    # The same choice, taken: only now is the work gone.
    _click_sign_out(page)
    first.wait_for()
    first.get_by_role("button", name="Sign out and remove them").click()
    warning.wait_for()
    warning.get_by_role("button", name="Remove anyway").click()

    page.wait_for_url(SIGN_OUT_URL)
    assert _idb_all(page, "decks") == []
    assert _idb_all(page, "outbox_reviews") == []
    assert _idb_all(page, "local_cards") == []
    assert _flag(page) is None
    # The server never got the review: that is what the user chose.
    assert not _reviews_of(wipe_server, qid)


def test_keeping_the_unsaved_work_still_signs_the_user_out(wipe_server, wipe_page):
    """The second dialog answers the DATA question. Its cancel used to
    answer the session question too, leaving a user who walked away
    from a shared machine still authenticated."""
    wipe_server.start()
    page = wipe_page
    base = wipe_server.base_url
    prefix = _prime(page, base)
    qid = wipe_server.seed["qids"][0]

    assert page.evaluate(
        _SEED_OUTBOX_JS, {"prefix": prefix, "questionId": qid, "prompt": AUTHORED_PROMPT}
    ) == [1, 1]
    page.route(SYNC_URL, lambda route: route.abort())
    _watch_sign_out(page)

    _click_sign_out(page)
    first = page.locator("dialog.offline-signout-dialog[open]")
    first.wait_for()
    first.get_by_role("button", name="Sign out and remove them").click()

    warning = page.locator("dialog.offline-unsynced-dialog[open]")
    warning.wait_for()
    warning.get_by_role("button", name="Sign out, keep the cards").click()

    page.wait_for_url(SIGN_OUT_URL)
    # Signed out, and nothing paid for it.
    assert len(_idb_all(page, "outbox_reviews")) == 1
    assert len(_idb_all(page, "local_cards")) == 1
    assert len(_idb_all(page, "decks")) >= 1
    assert _flag(page) == "1"


def test_escape_cannot_resolve_a_removal_that_is_still_running(wipe_server, wipe_page):
    """Esc closes every open dialog through a direct .close()
    (modules/details-toggle.js), which fires no cancel event. A choice
    that resolved on that would report the sign-out cancelled while its
    wipe ran on in the background."""
    wipe_server.start()
    page = wipe_page
    base = wipe_server.base_url
    prefix = _prime(page, base)
    qid = wipe_server.seed["qids"][1]

    assert page.evaluate(
        _SEED_OUTBOX_JS, {"prefix": prefix, "questionId": qid, "prompt": AUTHORED_PROMPT}
    ) == [1, 1]
    # Never answered, so the choice stays mid-flight while Esc lands.
    page.route(SYNC_URL, lambda route: None)
    requested = _watch_sign_out(page)

    _click_sign_out(page)
    dialog = page.locator("dialog.offline-signout-dialog[open]")
    dialog.wait_for()
    dialog.get_by_role("button", name="Sign out and remove them").click()
    page.wait_for_selector("dialog.offline-signout-dialog[data-busy]")

    page.keyboard.press("Escape")
    _settle(page)

    # Still deciding: the dialog is up, nothing is destroyed, and
    # nobody is signed out.
    assert dialog.count() == 1
    assert len(_idb_all(page, "decks")) >= 1
    assert len(_idb_all(page, "outbox_reviews")) == 1
    assert len(_idb_all(page, "local_cards")) == 1
    assert requested == []


def test_a_permanently_rejected_row_is_named_before_the_wipe(wipe_server, wipe_page):
    """`wipeAll` clears `rejects` with every other store, and no flush
    can save what the server already refused. The second dialog is the
    only place it can be named."""
    wipe_server.start()
    page = wipe_page
    base = wipe_server.base_url
    prefix = _prime(page, base)

    assert page.evaluate(_SEED_REJECT_JS, {"prefix": prefix}) == 1
    requested = _watch_sign_out(page)

    _click_sign_out(page)
    dialog = page.locator("dialog.offline-signout-dialog[open]")
    dialog.wait_for()
    dialog.get_by_role("button", name="Sign out and remove them").click()

    warning = page.locator("dialog.offline-unsynced-dialog[open]")
    warning.wait_for()
    text = warning.inner_text()
    assert "1 item could not be saved to your account." in text
    # The flush reached the server, so the network line would be false.
    assert "The server would not take that work" in text

    warning.get_by_role("button", name="Cancel").click()
    warning.wait_for(state="detached")
    _settle(page)
    assert len(_idb_all(page, "rejects")) == 1
    assert requested == []


def test_a_row_written_after_a_wipe_never_reaches_the_next_account(wipe_server, wipe_page):
    """A wipe clears `meta.owner`, and sync.js reads an absent owner as
    a pass. Anything a second tab writes after the wipe therefore
    belongs to nobody, and an offline-authored card carries only prompt
    and answer: the server would create it outright under whoever signs
    in next."""
    wipe_server.start()
    page = wipe_page
    base = wipe_server.base_url
    prefix = _prime(page, base)
    orphan = "Orphaned after the wipe"

    result = page.evaluate(_POST_WIPE_JS, {"prefix": prefix, "prompt": orphan})

    # The row existed, so an empty device afterwards is a result.
    assert result["before"] == 1
    # The harm, first: the row never became a question in the account
    # the server resolves for this browser.
    assert not _questions_with(wipe_server, orphan)
    # Dropped rather than left to be adopted later, and the device is
    # still un-owned: only a snapshot refresh may stamp one.
    assert result["after"] == 0
    assert result["owner"] is None


def test_a_wiped_device_syncs_again_once_it_has_an_owner(wipe_server, wipe_page):
    """The other half of the post-wipe marker: it has to come off. The
    marker makes flushOutbox drop every queued row, and only the
    refresh that stamps an owner clears it, so a marker that survives
    discards the user's offline work for the life of the device."""
    wipe_server.start()
    page = wipe_page
    base = wipe_server.base_url
    prefix = _prime(page, base)
    kept = "Written after the device came back"

    result = page.evaluate(_WIPE_THEN_RESYNC_JS, {"prefix": prefix, "prompt": kept})

    assert result["markerAfterWipe"] is not None
    assert result["markerAfterRefresh"] is None
    assert result["owner"]
    # The queue drained because the server took the row, not because
    # the marker dropped it.
    assert result["after"] == 0
    assert _questions_with(wipe_server, kept)


# ---- the landing page's second exit -------------------------------------


def _landing_records() -> dict:
    due = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    cards = [
        {
            "question_id": 700 + i,
            "deck_id": LANDING_DECK_ID,
            "type": "short",
            "prompt": prompt,
            "choices": None,
            "answer": answer,
            "answer_regex": answer.lower(),
            "step": 0,
            "next_due": due,
            "local_step": None,
            "local_next_due": None,
        }
        for i, (prompt, answer) in enumerate(
            [("Capital of Kenya?", "Nairobi"), ("Capital of Chile?", "Santiago")]
        )
    ]
    return {
        "decks": [
            {
                "id": LANDING_DECK_ID,
                "name": "saved-capitals",
                "display_name": LANDING_DECK_LABEL,
                "pinned_at": None,
                "total": len(cards),
            }
        ],
        "cards": cards,
        "owner": {
            "user_id": "device-wipe-owner",
            "display_name": "Device Owner",
            "snapshot_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "build": None,
        },
    }


def test_the_landing_status_line_says_where_the_cards_are_and_removes_them(
    offline_server, landing_page
):
    offline_server.start()
    page = landing_page
    base = offline_server.base_url

    page.goto(base + "/")
    page.locator("[data-landing-splash]").wait_for()
    page.evaluate(_SEED_LANDING_JS, {"prefix": _module_prefix(page), **_landing_records()})

    page.goto(base + "/")
    row = page.locator(".deck-list .deck-card").first
    row.wait_for()
    assert LANDING_DECK_LABEL.lower() in row.inner_text().lower()

    status = page.locator(".dashboard-status")
    text = status.inner_text()
    assert "These cards are saved on this browser" in text
    assert "anyone who uses it can open them" in text

    page.locator(".dashboard-status-action").click()
    dialog = page.locator("dialog.offline-remove-dialog[open]")
    dialog.wait_for()
    # True of a wipe: the server keeps whatever it already has.
    assert "Nothing is deleted from the account they came from." in dialog.inner_text()
    assert _choices(dialog) == ["Remove them", "Cancel"]
    dialog.get_by_role("button", name="Remove them").click()

    # The device holds nothing, so the page it should be on is the one
    # the server renders for everybody else.
    page.wait_for_selector("[data-landing-splash]", state="visible")
    assert _idb_all(page, "decks") == []
    assert _idb_all(page, "cards") == []
    assert _flag(page) is None
    assert page.locator(".deck-list .deck-card").count() == 0
    assert LANDING_DECK_LABEL not in page.locator("body").inner_text()


def test_the_landing_removal_can_be_cancelled(offline_server, landing_page):
    """The quiet action is one tap from the decks it destroys, so its
    cancel has to be real."""
    offline_server.start()
    page = landing_page
    base = offline_server.base_url

    page.goto(base + "/")
    page.locator("[data-landing-splash]").wait_for()
    page.evaluate(_SEED_LANDING_JS, {"prefix": _module_prefix(page), **_landing_records()})

    page.goto(base + "/")
    page.locator(".deck-list .deck-card").first.wait_for()
    page.locator(".dashboard-status-action").click()
    dialog = page.locator("dialog.offline-remove-dialog[open]")
    dialog.wait_for()
    dialog.get_by_role("button", name="Cancel").click()
    dialog.wait_for(state="detached")

    assert len(_idb_all(page, "decks")) == 1
    assert len(_idb_all(page, "cards")) == 2
    assert _flag(page) == "1"
    assert page.locator(".deck-list .deck-card").count() == 1
