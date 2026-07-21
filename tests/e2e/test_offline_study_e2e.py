"""End-to-end offline study (docs/OFFLINE.md section 7, M3 scope).

Drives the full study-offline-sync-later loop against a LOCAL
tailscale-mode prep instance (the offline_server fixture): prime the
snapshot + service worker online, kill the server and cold-navigate to
start_url (the only offline simulation that reaches the SW; Chromium's
offline emulation does not apply to service-worker fetches), study all
three seeded card shapes offline (mcq auto-grade, regex short
auto-grade, plain short reveal + self-verdict), then bring the server
back and assert the outbox replays into the real reviews log while the
local ladder overlays converge back to server truth.

Also pins, at the module level in a real browser, the M2-review trap:
a forced snapshot refresh after a PARTIAL flush must preserve the
local ladder overlays for cards that still have queued outbox reviews,
and only for those.
"""

from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timezone

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.browser]

# Every timestamp the offline app writes must be uniform-offset UTC in
# Date.prototype.toISOString() shape, because flushOutbox orders the
# outbox by LEXICOGRAPHIC reviewed_at comparison (trap (b) of the M2
# review). Pinned here at the observable write sites.
_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

# ---- IndexedDB readouts (evaluated in the page) -----------------------

_IDB_GETALL_JS = """
async (storeName) => new Promise((resolve, reject) => {
  const req = indexedDB.open("prep-offline");
  req.onsuccess = () => {
    const db = req.result;
    if (!db.objectStoreNames.contains(storeName)) { db.close(); resolve([]); return; }
    const tx = db.transaction(storeName, "readonly");
    const getAll = tx.objectStore(storeName).getAll();
    getAll.onsuccess = () => { db.close(); resolve(getAll.result); };
    getAll.onerror = () => { db.close(); reject(getAll.error); };
  };
  req.onerror = () => reject(req.error);
})
"""

_IDB_META_GET_JS = """
async (name) => new Promise((resolve) => {
  const req = indexedDB.open("prep-offline");
  req.onsuccess = () => {
    const db = req.result;
    if (!db.objectStoreNames.contains("meta")) { db.close(); resolve(null); return; }
    const tx = db.transaction("meta", "readonly");
    const get = tx.objectStore("meta").get(name);
    get.onsuccess = () => { db.close(); resolve(get.result ?? null); };
    get.onerror = () => { db.close(); resolve(null); };
  };
  req.onerror = () => resolve(null);
})
"""

# Polls (inside the page, so one evaluate round-trip) until the SW
# install has stored the offline shell at its build-stamped key.
_SHELL_CACHED_JS = """
async () => {
  for (let i = 0; i < 100; i++) {
    const names = await caches.keys();
    for (const n of names) {
      const c = await caches.open(n);
      const keys = await c.keys();
      if (keys.some((k) => k.url.includes("/offline?build="))) return true;
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}
"""


def _idb_all(page, store: str) -> list[dict]:
    return page.evaluate(_IDB_GETALL_JS, store)


def _wait_for(predicate, timeout: float = 20.0, interval: float = 0.25, message: str = ""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    pytest.fail(f"timed out waiting for {message or predicate}")


def _prime_online(page, base_url: str) -> None:
    """Load the dashboard online on a FRESH (uncontrolled) page: app.js
    registers the SW (which precaches the offline shell) and sync.js
    writes the snapshot into IndexedDB. Both must land before any
    offline navigation. Priming must happen on this first load: once
    the SW controls the page, navigations lose the route-injected auth
    header (the SW re-issues them itself, outside Playwright routing).
    """
    page.goto(base_url + "/")
    assert page.evaluate("() => navigator.serviceWorker.ready.then(() => true)") is True
    assert page.evaluate(_SHELL_CACHED_JS) is True, "SW install never cached the offline shell"
    _wait_for(
        lambda: page.evaluate(_IDB_META_GET_JS, "owner"),
        message="snapshot owner record in IndexedDB",
    )
    _wait_for(
        lambda: len(_idb_all(page, "cards")) == 3,
        message="3 snapshot cards in IndexedDB",
    )


def _module_prefix(page) -> str:
    """The versioned '@/' URL prefix from the page's importmap."""
    prefix = page.evaluate(
        "() => JSON.parse(document.querySelector('script[type=importmap]').textContent)"
        ".imports['@/']"
    )
    assert prefix
    return prefix


def _study_root_text(page) -> str:
    return page.locator("#offline-root").inner_text()


# ---- the full loop ----------------------------------------------------


def test_offline_study_and_reconnect_sync(offline_server, offline_ctx, offline_page):
    base = offline_server.base_url
    seed = offline_server.seed
    page = offline_page
    offline_server.start()  # idempotent; heals a prior test's failure state

    # -- prime: dashboard online seeds SW cache + IDB snapshot ---------
    _prime_online(page, base)

    # -- offline: dead server (reaches the SW) + emulation (fires the
    #    window offline/online events the shell listens for) ----------
    offline_server.stop()
    offline_ctx.set_offline(True)
    page.goto(base + "/")

    # The offline app rendered from the SW cache at start_url: not a
    # browser error page, not the landing page, and the URL did not
    # change (the shell is a navigation FALLBACK, not a redirect).
    page.wait_for_selector("[data-offline-root] .prelude")
    assert page.url.rstrip("/") == base
    lede = page.locator(".lede").inner_text()
    assert "Studying as Offline Tester" in lede
    assert "3 cards are due right now" in lede

    # -- card 1: mcq, deterministic auto-grade -------------------------
    page.get_by_role("button", name="Study").click()
    page.wait_for_selector(".study-card")
    assert page.locator(".study-prompt").inner_text() == "Capital of France?"
    page.locator("label.choice", has_text="Paris").click()
    page.get_by_role("button", name="Submit").click()
    page.wait_for_selector("h1.verdict-headline")
    assert page.locator("h1.verdict-headline").inner_text() == "Right."
    verdict_sub = page.locator(".verdict-sub").inner_text()
    assert "1 day" in verdict_sub
    assert "offline schedule" in verdict_sub

    outbox = _idb_all(page, "outbox_reviews")
    assert len(outbox) == 1
    row = outbox[0]
    assert row["question_id"] == seed["mcq_id"]
    assert row["verdict"] == "right"
    assert row["graded_by"] == "auto"
    assert row["user_answer"] == "Paris"
    assert row["client_id"]
    assert _ISO_Z.match(row["reviewed_at"]), row["reviewed_at"]

    cards = {c["question_id"]: c for c in _idb_all(page, "cards")}
    mcq_card = cards[seed["mcq_id"]]
    assert mcq_card["local_step"] == 1
    assert _ISO_Z.match(mcq_card["local_next_due"]), mcq_card["local_next_due"]
    due_at = datetime.fromisoformat(mcq_card["local_next_due"].replace("Z", "+00:00"))
    minutes_out = (due_at - datetime.now(timezone.utc)).total_seconds() / 60
    assert 1430 <= minutes_out <= 1450  # step 0 -> 1 is the 1-day rung

    # -- overlay re-surfacing: the studied card left the due queue ----
    page.locator("button.back").click()  # Pause, back to overview
    page.wait_for_selector(".offline-due-list")
    lede = page.locator(".lede").inner_text()
    assert "2 cards are due right now" in lede
    assert "Capital of France?" not in page.locator(".offline-due-list").inner_text()
    assert "1 review waiting to sync." in _study_root_text(page)

    # -- card 2: short with usable answer_regex, auto-grade ------------
    page.get_by_role("button", name="Study").click()
    page.wait_for_selector(".study-card")
    assert page.locator(".study-prompt").inner_text() == "Capital of Peru?"
    page.locator("textarea").fill("Lima")
    page.get_by_role("button", name="Submit").click()
    # Straight to the verdict view: no reveal step for a regex hit.
    page.wait_for_selector("h1.verdict-headline")
    assert page.locator("h1.verdict-headline").inner_text() == "Right."
    assert len(_idb_all(page, "outbox_reviews")) == 2

    # -- card 3: plain short, reveal + self-verdict --------------------
    page.get_by_role("button", name="Next card").click()
    page.wait_for_selector(".study-card")
    assert page.locator(".study-prompt").inner_text() == "What does the acronym SRS stand for?"
    page.locator("textarea").fill("A spaced repetition system.")
    page.get_by_role("button", name="Submit").click()
    page.wait_for_selector(".offline-selfgrade-blurb")
    # inner_text reflects rendered text, and the section eyebrows are
    # CSS-uppercased; compare case-insensitively.
    reveal_text = _study_root_text(page)
    assert "canonical answer" in reveal_text.lower()
    assert "Spaced repetition system." in reveal_text
    page.get_by_role("button", name="I got it right").click()
    page.wait_for_selector("h1.verdict-headline")
    assert page.locator("h1.verdict-headline").inner_text() == "Right."

    outbox = sorted(_idb_all(page, "outbox_reviews"), key=lambda r: r["reviewed_at"])
    assert len(outbox) == 3
    assert [r["question_id"] for r in outbox] == [
        seed["mcq_id"],
        seed["regex_id"],
        seed["short_id"],
    ]
    assert [r["graded_by"] for r in outbox] == ["auto", "auto", "self"]
    assert all(_ISO_Z.match(r["reviewed_at"]) for r in outbox)

    # -- queue drained ------------------------------------------------
    page.get_by_role("button", name="Next card").click()
    page.wait_for_selector(".empty-state")
    assert "All caught up" in _study_root_text(page)
    page.get_by_role("button", name="Back to overview").click()
    page.wait_for_selector(".offline-due")
    root_text = _study_root_text(page)
    assert "Nothing is due right now" in root_text
    assert "3 reviews waiting to sync." in root_text

    # -- reconnect: server back, then the online event -----------------
    offline_server.start()
    offline_ctx.set_offline(False)

    # The outbox replays through the real scheduler into the reviews
    # log, with the offline grader-notes markers and the client's
    # (lexicographically ordered) timestamps.
    #
    # The poll MUST make a Playwright call each iteration: with the
    # sync API, ctx.route() handlers only dispatch while the test
    # thread is inside a Playwright call, so a sqlite-only wait loop
    # would starve the very fetches (healthz probe, snapshot, sync
    # POST) it is waiting on.
    def _server_reviews():
        page.evaluate("() => 0")  # pump the event loop for route handlers
        conn = sqlite3.connect(offline_server.db_path)
        try:
            rows = conn.execute(
                "SELECT question_id, result, grader_notes, ts FROM reviews ORDER BY ts"
            ).fetchall()
        finally:
            conn.close()
        return rows if len(rows) == 3 else None

    rows = _wait_for(_server_reviews, timeout=30, message="3 review rows on the server")
    assert [r[0] for r in rows] == [seed["mcq_id"], seed["regex_id"], seed["short_id"]]
    assert [r[1] for r in rows] == ["right", "right", "right"]
    assert [r[2] for r in rows] == [
        "(offline auto)",
        "(offline auto)",
        "(offline self-graded)",
    ]

    # Client side converges: outbox empty, overlays cleared by the
    # forced post-flush snapshot refresh (nothing queued anymore), and
    # the studied cards now carry the server's FSRS next_due (future).
    _wait_for(
        lambda: len(_idb_all(page, "outbox_reviews")) == 0,
        message="outbox drained after reconnect flush",
    )
    cards = {c["question_id"]: c for c in _idb_all(page, "cards")}
    now = datetime.now(timezone.utc)
    for qid in (seed["mcq_id"], seed["regex_id"], seed["short_id"]):
        assert cards[qid]["local_step"] is None
        assert cards[qid]["local_next_due"] is None
        next_due = datetime.fromisoformat(cards[qid]["next_due"])
        assert next_due > now

    # The overview re-rendered without the waiting-to-sync note.
    _wait_for(
        lambda: "waiting to sync" not in _study_root_text(page),
        message="outbox note gone from the overview",
    )
    assert "Nothing is due right now" in _study_root_text(page)


# ---- the M2-review trap, pinned at module level -----------------------

_PRESERVE_OVERLAYS_JS = """
async ({prefix, regexId, mcqId}) => {
  const store = await import(prefix + "offline/store.js");
  const sync = await import(prefix + "offline/sync.js");
  const cards = await store.getAll("cards");
  const rcard = cards.find((c) => c.question_id === regexId);
  const mcard = cards.find((c) => c.question_id === mcqId);
  if (!rcard || !mcard) return {error: "seed cards missing from snapshot"};

  // Simulate the post-PARTIAL-flush world: one card still has a
  // queued review (its overlay must survive the refresh), one card's
  // reviews have all flushed (its overlay must converge to server
  // truth, i.e. null).
  await store.put("outbox_reviews", {
    client_id: store.uuid(),
    question_id: regexId,
    verdict: "right",
    user_answer: "planted",
    graded_by: "auto",
    reviewed_at: new Date().toISOString(),
  });
  await store.put("cards", {...rcard, local_step: 3, local_next_due: "2035-01-01T00:00:00.000Z"});
  await store.put("cards", {...mcard, local_step: 2, local_next_due: "2035-01-01T00:00:00.000Z"});

  const refreshed = await sync.refreshSnapshot({force: true});
  const after = await store.getAll("cards");
  const keptCard = after.find((c) => c.question_id === regexId);
  const wipedCard = after.find((c) => c.question_id === mcqId);

  // Drain the planted row and refresh again: with nothing queued the
  // kept overlay must clear too (the synced-card convergence half).
  for (const row of await store.getAll("outbox_reviews")) {
    await store.remove("outbox_reviews", row.client_id);
  }
  const refreshedAgain = await sync.refreshSnapshot({force: true});
  const finalCards = await store.getAll("cards");
  const clearedCard = finalCards.find((c) => c.question_id === regexId);

  return {
    refreshOk: Boolean(refreshed && refreshed.ok && refreshedAgain && refreshedAgain.ok),
    kept: {step: keptCard.local_step, due: keptCard.local_next_due},
    wiped: {step: wipedCard.local_step, due: wipedCard.local_next_due},
    cleared: {step: clearedCard.local_step, due: clearedCard.local_next_due},
  };
}
"""


# ---- M5: owner-mismatch confirm-then-wipe, pinned at module level -----

_OWNER_CONFLICT_SETUP_JS = """
async ({prefix, mcqId}) => {
  const store = await import(prefix + "offline/store.js");
  const sync = await import(prefix + "offline/sync.js");
  // Plant a FOREIGN owner under the real session: the server still
  // resolves the seeded e2e user, so the next identity-bearing sync
  // call must trip the guard.
  const owner = await store.metaGet("owner");
  await store.put(
    "meta",
    {...owner, user_id: "other-account@example.com", display_name: "Somebody Else"},
    "owner"
  );
  await store.put("outbox_reviews", {
    client_id: store.uuid(),
    question_id: mcqId,
    verdict: "right",
    user_answer: "planted-by-owner-test",
    graded_by: "auto",
    reviewed_at: new Date().toISOString(),
  });
  const flushResult = await sync.flushOutbox();
  const outboxAfterFlush = await store.getAll("outbox_reviews");
  const ownerAfterFlush = await store.metaGet("owner");
  // The guard alone must neither wipe nor prompt: refusing is all it
  // does. The dialog only appears through the explicit confirm call.
  const dialogAfterFlush = Boolean(document.querySelector("dialog.offline-owner-dialog"));
  const prompted = await sync.maybeConfirmOwnerConflict();
  return {
    disabled: Boolean(flushResult.disabled),
    outboxLenAfterFlush: outboxAfterFlush.length,
    ownerAfterFlush: ownerAfterFlush && ownerAfterFlush.user_id,
    dialogAfterFlush,
    prompted,
  };
}
"""

_OWNER_CONFLICT_REPROMPT_JS = """
async ({prefix}) => {
  const store = await import(prefix + "offline/store.js");
  const sync = await import(prefix + "offline/sync.js");
  // The recorded "keep" must suppress the prompt for this account...
  const suppressed = await sync.maybeConfirmOwnerConflict();
  const dialogWhileSuppressed = Boolean(document.querySelector("dialog.offline-owner-dialog"));
  // ...and only the recorded choice suppresses it: with the flag
  // gone (stand-in for a DIFFERENT mismatched account, whose id
  // would not match dismissed_user_id) the dialog re-opens.
  await store.remove("meta", "owner_conflict");
  const reprompted = await sync.maybeConfirmOwnerConflict();
  return {suppressed, dialogWhileSuppressed, reprompted};
}
"""


def test_owner_mismatch_confirm_then_wipe(offline_server, offline_page):
    """M5's confirm-then-wipe (docs/OFFLINE.md sections 3 and 6),
    against the real modules and the real dialog DOM: the guard
    refuses without wiping or prompting on its own; the dialog is
    explicit; Keep records the choice (no re-prompt for the same
    account) and discards nothing; Wipe clears every store and
    reseeds as the signed-in account."""
    from tests.e2e.conftest import OFFLINE_E2E_LOGIN

    offline_server.start()  # idempotent; heals a prior test's failure state
    page = offline_page
    _prime_online(page, offline_server.base_url)
    prefix = _module_prefix(page)

    setup = page.evaluate(
        _OWNER_CONFLICT_SETUP_JS,
        {"prefix": prefix, "mcqId": offline_server.seed["mcq_id"]},
    )
    # The guard tripped, refused the flush, and touched nothing.
    assert setup["disabled"] is True
    assert setup["outboxLenAfterFlush"] == 1
    assert setup["ownerAfterFlush"] == "other-account@example.com"
    assert setup["dialogAfterFlush"] is False, "guard must not prompt by itself"
    assert setup["prompted"] is True

    dialog = page.locator("dialog.offline-owner-dialog[open]")
    dialog.wait_for()
    dialog_text = dialog.inner_text()
    assert "Somebody Else" in dialog_text
    assert "1 unsynced review" in dialog_text
    assert "discarded" in dialog_text

    # -- Keep: records the choice, discards nothing --------------------
    page.get_by_role("button", name="Keep").click()
    page.locator("dialog.offline-owner-dialog").wait_for(state="detached")
    conflict_flag = page.evaluate(_IDB_META_GET_JS, "owner_conflict")
    assert conflict_flag and conflict_flag["dismissed_user_id"] == OFFLINE_E2E_LOGIN
    assert len(_idb_all(page, "outbox_reviews")) == 1
    owner = page.evaluate(_IDB_META_GET_JS, "owner")
    assert owner["user_id"] == "other-account@example.com"

    reprompt = page.evaluate(_OWNER_CONFLICT_REPROMPT_JS, {"prefix": prefix})
    assert reprompt["suppressed"] is False
    assert reprompt["dialogWhileSuppressed"] is False
    assert reprompt["reprompted"] is True

    # -- Wipe and start fresh: every store cleared, reseeded ----------
    page.get_by_role("button", name="Wipe and start fresh").click()
    page.locator("dialog.offline-owner-dialog").wait_for(state="detached")

    def _reseeded():
        page.evaluate("() => 0")  # pump the event loop for route handlers
        record = page.evaluate(_IDB_META_GET_JS, "owner")
        return record if record and record.get("user_id") == OFFLINE_E2E_LOGIN else None

    owner = _wait_for(_reseeded, message="owner reseeded as the signed-in account")
    assert owner["display_name"] == "Offline Tester"
    assert _idb_all(page, "outbox_reviews") == []
    assert _idb_all(page, "local_cards") == []
    assert _idb_all(page, "rejects") == []
    assert len(_idb_all(page, "cards")) == 3
    assert len(_idb_all(page, "decks")) >= 1
    # The wipe took the keep-flag with it (meta cleared wholesale) and
    # a fresh device id was minted.
    assert page.evaluate(_IDB_META_GET_JS, "owner_conflict") is None
    device = page.evaluate(_IDB_META_GET_JS, "device")
    assert device and device["device_id"]


def test_snapshot_refresh_preserves_overlays_only_for_queued_cards(offline_server, offline_page):
    """Trap (a) from the M2 review, exercised against the real modules
    in a real browser: refreshSnapshot's full replace must preserve
    local_step/local_next_due for exactly the cards that still have
    queued outbox reviews. Without the preservation, the forced
    refresh after a partial flush would snap unsynced cards back to
    the server's stale next_due and resurface cards the user already
    studied offline."""
    offline_server.start()  # idempotent; heals a prior test's failure state
    page = offline_page
    _prime_online(page, offline_server.base_url)
    result = page.evaluate(
        _PRESERVE_OVERLAYS_JS,
        {
            "prefix": _module_prefix(page),
            "regexId": offline_server.seed["regex_id"],
            "mcqId": offline_server.seed["mcq_id"],
        },
    )
    assert "error" not in result, result
    assert result["refreshOk"] is True
    assert result["kept"] == {"step": 3, "due": "2035-01-01T00:00:00.000Z"}
    assert result["wiped"] == {"step": None, "due": None}
    assert result["cleared"] == {"step": None, "due": None}
