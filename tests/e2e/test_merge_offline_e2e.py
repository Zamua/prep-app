"""End-to-end merge on an offline device (docs/ANONYMOUS-ACCOUNTS.md
section 5, "The merge must be visible to the offline device").

A device that studied anonymously carries `meta.owner.user_id =
"anon:…"`. Signing in merges that account server side, so the very
next snapshot answers with a DIFFERENT id. Without `previous_ids` the
owner guard reads its own data as a stranger's, disables sync, and
offers a dialog whose primary option wipes the unflushed work. The
pin here is the whole chain, in the real browser: the merge runs off
the cookie, the snapshot carries the old id, the guard re-stamps, and
the queued review flushes with no dialog in the DOM.

sw.js is blocked here: the test needs repeated authenticated
NAVIGATIONS, and a controlling service worker re-issues them outside
Playwright's routing, dropping the injected identity header.
"""

from __future__ import annotations

import pytest

from tests.e2e.celld_node import identity_headers, mint_anon_cookie
from tests.e2e.test_offline_study_e2e import (
    _IDB_META_GET_JS,
    _idb_all,
    _module_prefix,
    _wait_for,
)

pytestmark = [pytest.mark.slow, pytest.mark.browser]

MERGE_LOGIN = "merge-e2e@example.com"
MERGE_NAME = "Merge Tester"
ANON_ID = "anon:" + "9f" * 16
ANON_DECK = "instant-9f3c"
ANON_PROMPT = "Capital of Kenya?"


@pytest.fixture()
def merge_ctx(browser_session, offline_server):
    """Header-injected context for this suite, with sw.js blocked."""
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
            route.continue_(
                headers={**request.headers, **identity_headers(MERGE_LOGIN, MERGE_NAME)}
            )
        else:
            route.continue_()

    ctx.route("**/*", _inject_header)
    ctx.route("**/sw.js", lambda route: route.abort())
    try:
        yield ctx
    finally:
        ctx.close()


def _anon_cookie_value() -> str:
    """Signed with the key the local node derives from its master key, so
    the node's own verify accepts it."""
    return mint_anon_cookie(ANON_ID)


def _seed_anon_account(server) -> int:
    """An anonymous account with one instant deck, the shape a visitor
    lands on before signing up. Returns the question id."""
    return server.seed_profile(ANON_ID, "merge_anon")["question_id"]


def _server_state(server, question_id: int) -> dict:
    """Where the row ended up, and what the directory recorded.

    The owner column is gone: a question lives in exactly one cell, so
    "who owns it" is which cell answers for it. The audit trail is the
    directory's, which is its own cell.
    """
    directory = server.dump_directory()
    owner = None
    for user in (MERGE_LOGIN, ANON_ID):
        if any(q["id"] == question_id for q in server.rows(user, "questions")):
            owner = user
            break
    reviews = len(
        [r for r in server.rows(MERGE_LOGIN, "reviews") if r["question_id"] == question_id]
    )
    return {
        "question_owner": owner,
        "reviews": reviews,
        "anon_rows": len([u for u in directory["users"] if u["id"] == ANON_ID]),
        "merges": [
            {
                "status": m["status"],
                "anon_user_id": m["anon_user_id"],
                "target_user_id": m["target_user_id"],
            }
            for m in directory["account_merges"]
        ],
    }


# Stamp the device as the anonymous account and queue a review of one
# of its cards: the state a visitor who studied offline arrives with.
_SEED_DEVICE_JS = """
async ({prefix, anonId, questionId}) => {
  const store = await import(prefix + "offline/store.js");
  const owner = await store.metaGet("owner");
  await store.withLock(async () => {
    await store.put("meta", {...owner, user_id: anonId, display_name: "Guest"}, "owner");
    await store.put("outbox_reviews", {
      client_id: store.uuid(),
      question_id: questionId,
      verdict: "right",
      user_answer: "Nairobi",
      graded_by: "auto",
      reviewed_at: new Date().toISOString(),
    });
  });
  return (await store.metaGet("owner")).user_id;
}
"""

_SNAPSHOT_JS = """
async () => {
  const r = await fetch("/api/offline/snapshot", {
    credentials: "same-origin",
    headers: {accept: "application/json"},
  });
  return r.json();
}
"""


def test_a_merged_device_flushes_with_no_dialog(offline_server, merge_ctx):
    """The C3 shape: unflushed work on a device whose owner stamp is
    the merged-away id survives the sign-in and syncs itself."""
    offline_server.start()  # idempotent; heals a prior test's failure state
    base = offline_server.base_url
    page = merge_ctx.new_page()

    qid = _seed_anon_account(offline_server)

    # -- the device: signed in once, then stamped as the anon account --
    page.goto(base + "/")
    prefix = _module_prefix(page)
    _wait_for(
        lambda: page.evaluate(_IDB_META_GET_JS, "owner"),
        message="snapshot owner record in IndexedDB",
    )
    stamped = page.evaluate(
        _SEED_DEVICE_JS, {"prefix": prefix, "anonId": ANON_ID, "questionId": qid}
    )
    assert stamped == ANON_ID
    assert len(_idb_all(page, "outbox_reviews")) == 1

    # -- sign in carrying the cookie: the merge runs on this request --
    merge_ctx.add_cookies([{"name": "prep_anon", "value": _anon_cookie_value(), "url": base}])
    page.goto(base + "/")

    _wait_for(
        lambda: len(_idb_all(page, "outbox_reviews")) == 0,
        message="the queued review to flush after the merge",
    )

    state = _server_state(offline_server, qid)
    assert state["question_owner"] == MERGE_LOGIN, "the anon deck did not move"
    assert state["anon_rows"] == 0
    assert state["reviews"] == 1, "the queued review never reached the server"
    # One navigation issues several authenticated requests, and every
    # one in flight when the merge lands still carries the cookie: the
    # extra attempts find no anonymous row and record it. Exactly one
    # of them moved data.
    statuses = [m["status"] for m in state["merges"]]
    assert statuses.count("completed") == 1
    assert set(statuses) <= {"completed", "failed"}
    assert {m["anon_user_id"] for m in state["merges"]} == {ANON_ID}

    # The guard read previous_ids, re-stamped, and never prompted.
    assert page.locator("dialog.offline-owner-dialog").count() == 0
    owner = page.evaluate(_IDB_META_GET_JS, "owner")
    assert owner["user_id"] == MERGE_LOGIN

    payload = page.evaluate(_SNAPSHOT_JS)
    assert payload["user"]["previous_ids"] == [ANON_ID]
    assert any(deck["name"] == ANON_DECK for deck in payload["decks"])


def test_an_unrelated_owner_still_hits_the_dialog(offline_server, merge_ctx):
    """previous_ids weakens nothing: an id in nobody's list is still a
    stranger, and the confirm-then-wipe dialog still fires."""
    offline_server.start()
    base = offline_server.base_url
    page = merge_ctx.new_page()
    qid = offline_server.seed["mcq_id"]

    page.goto(base + "/")
    prefix = _module_prefix(page)
    _wait_for(
        lambda: page.evaluate(_IDB_META_GET_JS, "owner"),
        message="snapshot owner record in IndexedDB",
    )
    page.evaluate(
        _SEED_DEVICE_JS,
        {"prefix": prefix, "anonId": "someone-else@example.com", "questionId": qid},
    )

    page.goto(base + "/")

    page.locator("dialog.offline-owner-dialog[open]").wait_for()
    assert len(_idb_all(page, "outbox_reviews")) == 1
    owner = page.evaluate(_IDB_META_GET_JS, "owner")
    assert owner["user_id"] == "someone-else@example.com"
