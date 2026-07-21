"""End-to-end M5 hardening (docs/OFFLINE.md section 8, M5 scope).

Three suites against the same LOCAL tailscale-mode prep instance the
study/authoring suites use (the offline_server fixture):

- The different-owner flow, driven end to end by real header
  identities: user A studies and authors offline, then user B signs
  in on the same device (same browser context, same IndexedDB; the
  injected Tailscale identity flips mid-test). The shell must sync
  NOTHING (zero POSTs to the sync endpoint, server rows untouched),
  surface the confirm-then-wipe dialog on reconnect, leave A's data
  intact with sync disabled on "keep", and on "wipe" clear every
  store and reseed as B while A's outbox never reaches the server.
- The needs-attention list: a review the server permanently rejected
  at sync surfaces on the offline overview (count summary, expandable
  row with kind / error / preview) and a per-row dismiss deletes it
  from the rejects store.
- Storage persistence: a successful snapshot write asks the platform
  for persistence exactly when not already granted (the no-nag skip
  is pinned too), and the overview renders the quiet storage readout
  line from the same API.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.e2e.conftest import OFFLINE_E2E_LOGIN, OFFLINE_E2E_NAME
from tests.e2e.test_offline_author_e2e import _reset_seed_due_times
from tests.e2e.test_offline_study_e2e import (
    _IDB_META_GET_JS,
    _idb_all,
    _module_prefix,
    _prime_online,
    _wait_for,
)

pytestmark = [pytest.mark.slow, pytest.mark.browser]

SECOND_LOGIN = "offline-second@example.com"
SECOND_NAME = "Second User"

# The card user A authors offline in the owner-switch test. It must
# never reach the server (that IS the assertion); unique enough that a
# leak is caught by the count check and greppable by prompt.
A_FRONT = "Capital of Mongolia?"
A_BACK = "Ulaanbaatar"


def _server_counts(db_path) -> tuple[int, int, int]:
    """The server-side rows A's outbox could create if the guard ever
    leaked: reviews, questions, idempotency pins."""
    conn = sqlite3.connect(db_path)
    try:
        n_reviews = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        n_questions = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        n_pins = conn.execute("SELECT COUNT(*) FROM offline_sync_idempotency").fetchone()[0]
    finally:
        conn.close()
    return (n_reviews, n_questions, n_pins)


# ---- the different-owner flow -----------------------------------------


@pytest.fixture()
def owner_switch_ctx(browser_session, offline_server):
    """Like offline_ctx, but the injected Tailscale identity is
    mutable mid-test (the yielded dict), so ONE browser context -- one
    device, one IndexedDB -- can be visited by two different signed-in
    users. Every POST the page makes to the sync endpoint is also
    recorded, so 'no sync happened' is a direct observation rather
    than an inference from server state. Exit restarts the server so
    a mid-test failure while 'offline' cannot leak a dead server into
    sibling tests."""
    identity = {"login": OFFLINE_E2E_LOGIN, "name": OFFLINE_E2E_NAME}
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

    ctx.route("**/*", _inject_header)
    # Registered second so it dispatches FIRST (Playwright checks the
    # most recently added matching handler first), then falls back.
    ctx.route("**/api/offline/sync", _count_sync_posts)
    try:
        yield ctx, identity, sync_posts
    finally:
        ctx.close()
        offline_server.start()


_SYNC_STAYS_DISABLED_JS = """
async ({prefix}) => {
  const sync = await import(prefix + "offline/sync.js");
  const flush = await sync.flushOutbox();
  const refresh = await sync.refreshSnapshot({force: true});
  const suppressed = await sync.maybeConfirmOwnerConflict();
  return {
    flushDisabled: Boolean(flush.disabled),
    refreshDisabled: Boolean(refresh.disabled),
    suppressed,
    dialogOpen: Boolean(document.querySelector("dialog.offline-owner-dialog")),
  };
}
"""

_REPROMPT_JS = """
async ({prefix}) => {
  const store = await import(prefix + "offline/store.js");
  const sync = await import(prefix + "offline/sync.js");
  await store.remove("meta", "owner_conflict");
  return sync.maybeConfirmOwnerConflict();
}
"""


def test_different_owner_reconnect_confirm_then_wipe(offline_server, owner_switch_ctx):
    """User A studies + authors offline; user B reconnects on the same
    device. Docs/OFFLINE.md sections 3 and 6: no sync (the guard
    refuses before any POST), an explicit dialog, keep = A's data
    intact + sync stays off, wipe = stores cleared and reseeded as B
    with the server never having seen A's outbox."""
    ctx, identity, sync_posts = owner_switch_ctx
    base = offline_server.base_url
    page = ctx.new_page()

    offline_server.start()  # idempotent; heals a prior test's failure state
    _reset_seed_due_times(offline_server)

    # -- prime online as A: SW cache + IDB snapshot owned by A ---------
    _prime_online(page, base)
    prefix = _module_prefix(page)

    # -- offline as A: study one card, author one card -----------------
    offline_server.stop()
    ctx.set_offline(True)
    page.goto(base + "/")
    page.wait_for_selector("[data-offline-root] .prelude")
    assert "Studying as Offline Tester" in page.locator(".lede").inner_text()

    page.get_by_role("button", name="Study").click()
    page.wait_for_selector(".study-card")
    page.locator("label.choice", has_text="Paris").click()
    page.get_by_role("button", name="Submit").click()
    page.wait_for_selector("h1.verdict-headline")
    page.locator("button.back").click()
    page.wait_for_selector(".offline-due")

    page.get_by_role("button", name="Add a card").click()
    page.wait_for_selector(".author-form")
    page.locator(".author-form textarea").fill(A_FRONT)
    page.locator(".author-form input[type=text]").fill(A_BACK)
    page.get_by_role("button", name="Save card").click()
    page.wait_for_selector(".offline-toast")

    assert len(_idb_all(page, "outbox_reviews")) == 1
    assert len(_idb_all(page, "local_cards")) == 1
    device_before = page.evaluate(_IDB_META_GET_JS, "device")
    assert device_before and device_before["device_id"]

    baseline = _server_counts(offline_server.db_path)

    # -- B signs in on the same device; connectivity returns -----------
    identity["login"] = SECOND_LOGIN
    identity["name"] = SECOND_NAME
    offline_server.start()
    ctx.set_offline(False)

    # The shell's reconnect flow trips the guard and surfaces the
    # dialog on its own (never silent, never automatic beyond this).
    dialog = page.locator("dialog.offline-owner-dialog[open]")
    dialog.wait_for(timeout=30_000)
    dialog_text = dialog.inner_text()
    assert "Offline Tester" in dialog_text  # whose data the device holds
    assert "Second User" in dialog_text  # who is signed in
    assert "1 unsynced review and 1 unsynced new card" in dialog_text
    assert "discarded" in dialog_text

    # No sync happened: zero POSTs, server rows untouched, A's local
    # data untouched, owner still A.
    assert sync_posts == []
    assert _server_counts(offline_server.db_path) == baseline
    assert len(_idb_all(page, "outbox_reviews")) == 1
    assert len(_idb_all(page, "local_cards")) == 1
    assert page.evaluate(_IDB_META_GET_JS, "owner")["user_id"] == OFFLINE_E2E_LOGIN

    # -- keep: A's data stays, sync stays disabled for the session -----
    page.get_by_role("button", name="Keep").click()
    page.locator("dialog.offline-owner-dialog").wait_for(state="detached")
    conflict_flag = page.evaluate(_IDB_META_GET_JS, "owner_conflict")
    assert conflict_flag and conflict_flag["dismissed_user_id"] == SECOND_LOGIN
    assert len(_idb_all(page, "outbox_reviews")) == 1
    assert len(_idb_all(page, "local_cards")) == 1
    assert page.evaluate(_IDB_META_GET_JS, "owner")["user_id"] == OFFLINE_E2E_LOGIN

    disabled = page.evaluate(_SYNC_STAYS_DISABLED_JS, {"prefix": prefix})
    assert disabled["flushDisabled"] is True
    assert disabled["refreshDisabled"] is True
    assert disabled["suppressed"] is False  # keep recorded: no re-prompt
    assert disabled["dialogOpen"] is False
    assert sync_posts == []
    assert _server_counts(offline_server.db_path) == baseline

    # -- wipe and start fresh: cleared, reseeded as B ------------------
    assert page.evaluate(_REPROMPT_JS, {"prefix": prefix}) is True
    page.get_by_role("button", name="Wipe and start fresh").click()
    page.locator("dialog.offline-owner-dialog").wait_for(state="detached")

    def _reseeded():
        page.evaluate("() => 0")  # pump the event loop for route handlers
        record = page.evaluate(_IDB_META_GET_JS, "owner")
        return record if record and record.get("user_id") == SECOND_LOGIN else None

    owner = _wait_for(_reseeded, message="owner reseeded as user B")
    assert owner["display_name"] == SECOND_NAME
    assert _idb_all(page, "outbox_reviews") == []
    assert _idb_all(page, "local_cards") == []
    assert _idb_all(page, "rejects") == []
    assert _idb_all(page, "cards") == []  # B has no cards server-side
    assert _idb_all(page, "decks") == []
    device_after = page.evaluate(_IDB_META_GET_JS, "device")
    assert device_after["device_id"] != device_before["device_id"]

    # A's outbox died with the wipe, never having touched the server:
    # zero sync POSTs across the whole test, counts unchanged, and A's
    # authored card does not exist anywhere server-side.
    assert sync_posts == []
    assert _server_counts(offline_server.db_path) == baseline
    conn = sqlite3.connect(offline_server.db_path)
    try:
        leaked = conn.execute(
            "SELECT COUNT(*) FROM questions WHERE prompt = ?", (A_FRONT,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert leaked == 0

    # -- the device now boots as B's ----------------------------------
    page.goto(base + "/offline")
    page.wait_for_selector("[data-offline-root] .prelude")
    lede = page.locator(".lede").inner_text()
    assert "Studying as Second User" in lede
    assert "Nothing is due right now" in lede


# ---- the needs-attention list -----------------------------------------

_SEED_REJECT_JS = """
async ({prefix}) => {
  const store = await import(prefix + "offline/store.js");
  const sync = await import(prefix + "offline/sync.js");
  await sync.refreshSnapshot({force: true});
  // A review whose question no longer exists server-side: the shape
  // from docs/OFFLINE.md section 6 ("Snapshot card deleted server-side
  // while its review was queued") -- permanently rejected at flush.
  await store.put("outbox_reviews", {
    client_id: store.uuid(),
    question_id: 99999999,
    verdict: "right",
    user_answer: "planted answer",
    graded_by: "auto",
    reviewed_at: new Date().toISOString(),
  });
  const flush = await sync.flushOutbox();
  return {
    flush,
    outboxLeft: (await store.getAll("outbox_reviews")).length,
    rejects: await store.getAll("rejects"),
  };
}
"""


def test_rejected_review_surfaces_and_dismisses(offline_server, offline_page):
    """A server-rejected item lands in the rejects store and the
    offline overview surfaces it (docs/OFFLINE.md section 2: kept in a
    needs-attention list rather than silently dropped): count on the
    summary, expandable row with kind / server error / preview, and a
    per-row dismiss that deletes it."""
    offline_server.start()  # idempotent; heals a prior test's failure state
    page = offline_page
    page.goto(offline_server.base_url + "/offline")

    seeded = page.evaluate(_SEED_REJECT_JS, {"prefix": _module_prefix(page)})
    assert seeded["flush"]["rejected"] == 1, seeded
    assert seeded["outboxLeft"] == 0
    assert len(seeded["rejects"]) == 1
    assert seeded["rejects"][0]["error"] == "unknown question_id"

    # -- the overview surfaces it --------------------------------------
    page.reload()
    page.wait_for_selector(".offline-rejects")
    summary = page.locator(".offline-rejects-summary")
    assert summary.inner_text() == "1 item couldn't sync"

    # Collapsed by default; the summary expands the list.
    row = page.locator(".offline-reject")
    assert not row.is_visible()
    summary.click()
    row.wait_for(state="visible")
    assert row.locator(".tag-type").inner_text() == "review"
    assert row.locator(".offline-reject-error").inner_text() == "unknown question_id"
    # The rejected question_id is not in the snapshot (that is why it
    # rejected), so the preview falls back to the carried answer.
    assert row.locator(".offline-reject-preview").inner_text() == "Your answer: planted answer"

    # -- dismiss deletes the row ---------------------------------------
    page.get_by_role("button", name="Dismiss").click()
    page.locator(".offline-rejects").wait_for(state="detached")
    assert _idb_all(page, "rejects") == []


# ---- storage persistence ----------------------------------------------

_STUB_STORAGE_JS = """
(() => {
  window.__persistCalls = 0;
  window.__persistedResult = false;
  if (!("storage" in navigator)) return;
  try {
    navigator.storage.persisted = () => Promise.resolve(window.__persistedResult);
    navigator.storage.persist = () => {
      window.__persistCalls += 1;
      return Promise.resolve(true);
    };
    navigator.storage.estimate = () => Promise.resolve({usage: 2048, quota: 1073741824});
  } catch (e) {
    // leave the real API in place; the assertions will say so
  }
})();
"""

_REFRESH_JS = """
async ({prefix}) => {
  const sync = await import(prefix + "offline/sync.js");
  return sync.refreshSnapshot({force: true});
}
"""

_REFRESH_WITH_GRANT_JS = """
async ({prefix}) => {
  window.__persistedResult = true;
  window.__persistCalls = 0;
  const sync = await import(prefix + "offline/sync.js");
  const result = await sync.refreshSnapshot({force: true});
  // requestPersistence is fire-and-forget; give its promise chain a
  // beat to run before reading the counter.
  await new Promise((r) => setTimeout(r, 100));
  return {ok: Boolean(result && result.ok), calls: window.__persistCalls};
}
"""


def test_snapshot_write_requests_storage_persistence(offline_server, offline_ctx):
    """docs/OFFLINE.md section 3 (storage persistence and eviction
    margin): a successful snapshot write asks the platform to persist
    this origin's storage, skips the ask when the grant already
    exists, and the overview renders the quiet storage readout from
    the same API."""
    offline_server.start()  # idempotent; heals a prior test's failure state
    page = offline_ctx.new_page()
    page.add_init_script(_STUB_STORAGE_JS)
    page.goto(offline_server.base_url + "/offline")
    prefix = _module_prefix(page)

    # -- a successful snapshot write asks for persistence --------------
    first = page.evaluate(_REFRESH_JS, {"prefix": prefix})
    assert first["ok"] is True, first
    _wait_for(
        lambda: page.evaluate("() => window.__persistCalls") >= 1,
        message="navigator.storage.persist() invoked after the snapshot write",
    )

    # -- already persisted: no re-ask (the no-nag guard) ---------------
    granted = page.evaluate(_REFRESH_WITH_GRANT_JS, {"prefix": prefix})
    assert granted["ok"] is True, granted
    assert granted["calls"] == 0

    # -- the overview's quiet readout line ------------------------------
    page.reload()  # boots the shell against the seeded snapshot
    page.wait_for_selector(".offline-storage-note")
    note = page.locator(".offline-storage-note").inner_text()
    assert note == "Offline storage: 2 KB used."

    # With the grant in place the line says so.
    page.add_init_script("window.__persistedResult = true;")
    page.reload()
    page.wait_for_selector(".offline-storage-note")
    note = page.locator(".offline-storage-note").inner_text()
    assert note == "Offline storage: 2 KB used · persistent."
