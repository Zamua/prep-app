"""End-to-end offline authoring (docs/OFFLINE.md section 7, M4 scope).

Drives the complete v1 authoring story against the same LOCAL
tailscale-mode prep instance the study suite uses (the offline_server
fixture): prime online, kill the server and cold-navigate to start_url
(reaching the SW's navigation fallback), author a card through the
form flow (validation refusals pinned on the way), watch it join the
due queue immediately, study it as a reveal + self-verdict card, then
reconnect and assert the sync path end to end on the server: a
type='short' question created in the get-or-created SRS inbox deck, a
review row resolved through card_client_id with the offline
self-graded marker, FSRS state initialized by the replay, and full
idempotency (a forced second flush of the same client rows leaves
zero duplicates).

Server rows created here are purged on teardown so the session-scoped
offline_server database stays exactly as seeded for the sibling
offline suites (their primes and review counts assume the 3-card
seed).
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from tests.e2e.test_offline_study_e2e import (
    _ISO_Z,
    _idb_all,
    _module_prefix,
    _prime_online,
    _study_root_text,
    _wait_for,
)

pytestmark = [pytest.mark.slow, pytest.mark.browser]

# The authored card. Generic learning content, unique enough that the
# server-side purge can key on the prompt alone.
AUTHOR_FRONT = "Capital of Iceland?"
AUTHOR_BACK = "Reykjavik"
AUTHOR_ANSWER_TYPED = "Reykjavik, the northernmost capital."

_DUE_RE = re.compile(r"(\d+) cards? (?:is|are) due right now")


def _due_count(page) -> int:
    lede = page.locator(".lede").inner_text()
    if "Nothing is due right now" in lede:
        return 0
    m = _DUE_RE.search(lede)
    assert m, f"could not parse due count from lede: {lede!r}"
    return int(m.group(1))


# ---- server-side purge -------------------------------------------------
#
# The offline_server database is session-scoped and the sibling suites
# assert exact seeded counts (3 snapshot cards, 3 review rows). Every
# row this suite creates is keyed off the authored prompt, so entry
# and exit both purge by prompt: the question, its cards/reviews rows,
# its idempotency pins, and the inbox deck when it ends up empty.


def _purge_authored(db_path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        qids = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM questions WHERE prompt = ?", (AUTHOR_FRONT,)
            ).fetchall()
        ]
        for qid in qids:
            conn.execute("DELETE FROM reviews WHERE question_id = ?", (qid,))
            conn.execute("DELETE FROM cards WHERE question_id = ?", (qid,))
            conn.execute("DELETE FROM offline_sync_idempotency WHERE question_id = ?", (qid,))
            conn.execute("DELETE FROM questions WHERE id = ?", (qid,))
        # Drop the auto-created inbox deck only when nothing else lives
        # in it (another suite must never lose a deck it created).
        conn.execute(
            "DELETE FROM decks WHERE name = 'inbox' "
            " AND id NOT IN (SELECT DISTINCT deck_id FROM questions WHERE deck_id IS NOT NULL)"
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def authored_rows_purged(offline_server):
    """Entry heal + exit cleanup around the authoring test. The exit
    half also restarts the server so a mid-test failure while
    'offline' (server stopped) cannot leak a dead server into the
    next test's fixtures."""
    _purge_authored(offline_server.db_path)
    yield
    offline_server.start()
    _purge_authored(offline_server.db_path)


# ---- idempotency replay (evaluated in the page) ------------------------
#
# After the first flush drains local_cards and outbox_reviews, the
# only way to force a genuine second flush of the SAME client rows is
# to reinsert them (same client_ids) and call flushOutbox again
# through the real module. The server must replay both from its
# idempotency pins: same outcome statuses, zero new rows.

_REFLUSH_JS = """
async ({prefix, localCard, outboxRow}) => {
  const store = await import(prefix + "offline/store.js");
  const sync = await import(prefix + "offline/sync.js");
  await store.put("local_cards", localCard);
  await store.put("outbox_reviews", outboxRow);
  const result = await sync.flushOutbox();
  return {
    result,
    remainingCards: (await store.getAll("local_cards")).length,
    remainingReviews: (await store.getAll("outbox_reviews")).length,
    rejects: (await store.getAll("rejects")).length,
  };
}
"""


def test_offline_author_study_and_reconnect_sync(
    offline_server, offline_ctx, offline_page, authored_rows_purged
):
    base = offline_server.base_url
    page = offline_page
    offline_server.start()  # idempotent; heals a prior test's failure state

    # -- prime: dashboard online seeds SW cache + IDB snapshot ---------
    _prime_online(page, base)

    # -- offline: dead server (reaches the SW) + emulation -------------
    offline_server.stop()
    offline_ctx.set_offline(True)
    page.goto(base + "/")
    page.wait_for_selector("[data-offline-root] .prelude")
    due_before = _due_count(page)

    # -- authoring form: entry point + deck picker ---------------------
    page.get_by_role("button", name="Add a card").click()
    page.wait_for_selector(".author-form")
    deck_options = page.locator(".author-form select option").all_inner_texts()
    assert deck_options[0] == "inbox (default)"
    assert "Offline E2E" in deck_options
    assert page.locator(".author-form select").input_value() == ""

    # -- validation: empty fields refuse -------------------------------
    page.get_by_role("button", name="Save card").click()
    page.wait_for_selector(".author-error", state="visible")
    assert page.locator(".author-error").inner_text() == "Both the front and the back are required."
    assert _idb_all(page, "local_cards") == []

    # Front alone still refuses (both are required), and nothing saves.
    page.locator(".author-form textarea").fill(AUTHOR_FRONT)
    page.get_by_role("button", name="Save card").click()
    page.wait_for_selector(".author-error", state="visible")
    assert _idb_all(page, "local_cards") == []

    # -- save: local_cards row + toast + back to overview --------------
    page.locator(".author-form input[type=text]").fill(AUTHOR_BACK)
    page.get_by_role("button", name="Save card").click()
    page.wait_for_selector(".offline-toast")
    assert page.locator(".offline-toast").inner_text() == "Card added"
    page.wait_for_selector(".offline-due")

    local_cards = _idb_all(page, "local_cards")
    assert len(local_cards) == 1
    local = local_cards[0]
    assert local["client_id"]
    assert local["deck_id"] is None  # inbox default
    assert local["prompt"] == AUTHOR_FRONT
    assert local["answer"] == AUTHOR_BACK
    assert _ISO_Z.match(local["created_at"]), local["created_at"]
    assert local["local_step"] == 0
    assert local["local_next_due"] is None  # null = due now

    # -- surfacing: unsynced count + due queue membership --------------
    assert "1 new card waiting to sync." in _study_root_text(page)
    assert _due_count(page) == due_before + 1
    assert AUTHOR_FRONT in page.locator(".offline-due-list").inner_text()

    # -- study it: null due sorts first, short reveal + self-verdict ---
    page.get_by_role("button", name="Study").click()
    page.wait_for_selector(".study-card")
    assert page.locator(".study-prompt").inner_text() == AUTHOR_FRONT
    assert page.locator(".card-id").inner_text() == "new card"
    assert page.locator(".tag-type").inner_text() == "short"
    page.locator("textarea").fill(AUTHOR_ANSWER_TYPED)
    page.get_by_role("button", name="Submit").click()
    page.wait_for_selector(".offline-selfgrade-blurb")
    reveal_text = _study_root_text(page)
    assert "canonical answer" in reveal_text.lower()
    assert AUTHOR_BACK in reveal_text
    page.get_by_role("button", name="I got it right").click()
    page.wait_for_selector("h1.verdict-headline")
    assert page.locator("h1.verdict-headline").inner_text() == "Right."
    verdict_sub = page.locator(".verdict-sub").inner_text()
    assert "1 day" in verdict_sub  # ladder step 0 -> 1
    assert "offline schedule" in verdict_sub

    # -- the outbox row targets card_client_id, never question_id ------
    outbox = _idb_all(page, "outbox_reviews")
    assert len(outbox) == 1
    review = outbox[0]
    assert review["card_client_id"] == local["client_id"]
    assert "question_id" not in review
    assert review["verdict"] == "right"
    assert review["graded_by"] == "self"
    assert review["user_answer"] == AUTHOR_ANSWER_TYPED
    assert _ISO_Z.match(review["reviewed_at"]), review["reviewed_at"]

    # -- the ladder overlay landed on the local_cards row --------------
    local_after = _idb_all(page, "local_cards")[0]
    assert local_after["local_step"] == 1
    assert _ISO_Z.match(local_after["local_next_due"]), local_after["local_next_due"]
    due_at = datetime.fromisoformat(local_after["local_next_due"].replace("Z", "+00:00"))
    minutes_out = (due_at - datetime.now(timezone.utc)).total_seconds() / 60
    assert 1430 <= minutes_out <= 1450  # the 1-day rung

    # -- overview: card left the due queue, both sync notes show -------
    page.locator("button.back").click()
    page.wait_for_selector(".offline-due")
    assert _due_count(page) == due_before
    root_text = _study_root_text(page)
    assert "1 review waiting to sync." in root_text
    assert "1 new card waiting to sync." in root_text

    # -- reconnect: server back, then the online event -----------------
    offline_server.start()
    offline_ctx.set_offline(False)

    # The poll MUST make a Playwright call each iteration: route
    # handlers only dispatch while the test thread is inside a
    # Playwright call (see the study suite's identical loop).
    def _server_question_and_review():
        page.evaluate("() => 0")  # pump the event loop for route handlers
        conn = sqlite3.connect(offline_server.db_path)
        try:
            qrows = conn.execute(
                "SELECT q.id, q.type, q.answer, q.user_id, d.name, "
                "       COALESCE(d.deck_type, 'srs') "
                "  FROM questions q JOIN decks d ON d.id = q.deck_id "
                " WHERE q.prompt = ?",
                (AUTHOR_FRONT,),
            ).fetchall()
            if len(qrows) != 1:
                return None
            rrows = conn.execute(
                "SELECT result, user_answer, grader_notes, ts FROM reviews "
                " WHERE question_id = ?",
                (qrows[0][0],),
            ).fetchall()
        finally:
            conn.close()
        return (qrows[0], rrows) if len(rrows) == 1 else None

    qrow, rrows = _wait_for(
        _server_question_and_review,
        timeout=30,
        message="authored question + its review on the server",
    )
    qid, qtype, qanswer, quser, deck_name, deck_type = qrow
    assert qtype == "short"
    assert qanswer == AUTHOR_BACK
    assert quser == "offline-e2e@example.com"
    assert deck_name == "inbox"  # deck_id null files into the SRS inbox
    assert deck_type == "srs"
    result, user_answer, grader_notes, review_ts = rrows[0]
    assert result == "right"
    assert user_answer == AUTHOR_ANSWER_TYPED
    assert grader_notes == "(offline self-graded)"
    assert datetime.fromisoformat(review_ts) == datetime.fromisoformat(
        review["reviewed_at"].replace("Z", "+00:00")
    )

    # -- FSRS state initialized by the replay --------------------------
    conn = sqlite3.connect(offline_server.db_path)
    try:
        card_row = conn.execute(
            "SELECT next_due, last_review, stability, difficulty, fsrs_state "
            "  FROM cards WHERE question_id = ?",
            (qid,),
        ).fetchone()
        pins = conn.execute(
            "SELECT client_id, kind, status, question_id "
            "  FROM offline_sync_idempotency WHERE question_id = ?",
            (qid,),
        ).fetchall()
    finally:
        conn.close()
    next_due, last_review, stability, difficulty, fsrs_state = card_row
    assert stability is not None
    assert difficulty is not None
    assert fsrs_state is not None
    assert datetime.fromisoformat(last_review) == datetime.fromisoformat(
        review["reviewed_at"].replace("Z", "+00:00")
    )
    assert datetime.fromisoformat(next_due) > datetime.now(timezone.utc)

    # Both idempotency pins landed: the created card and its review.
    assert {(p[0], p[1], p[2]) for p in pins} == {
        (local["client_id"], "card", "created"),
        (review["client_id"], "review", "applied"),
    }

    # -- client convergence: stores drained, snapshot delivers the card
    _wait_for(
        lambda: len(_idb_all(page, "local_cards")) == 0,
        message="local_cards drained after reconnect flush",
    )
    _wait_for(
        lambda: len(_idb_all(page, "outbox_reviews")) == 0,
        message="outbox drained after reconnect flush",
    )
    cards = {c["question_id"]: c for c in _idb_all(page, "cards")}
    assert qid in cards, "post-flush snapshot refresh should deliver the created card"
    assert cards[qid]["type"] == "short"
    assert cards[qid]["local_step"] is None
    assert cards[qid]["local_next_due"] is None
    assert datetime.fromisoformat(cards[qid]["next_due"]) > datetime.now(timezone.utc)
    assert "inbox" in {d["name"] for d in _idb_all(page, "decks")}
    _wait_for(
        lambda: "waiting to sync" not in _study_root_text(page),
        message="sync notes gone from the overview",
    )

    # -- idempotency: force a second flush of the SAME rows ------------
    reflush = page.evaluate(
        _REFLUSH_JS,
        {"prefix": _module_prefix(page), "localCard": local_after, "outboxRow": review},
    )
    assert reflush["result"]["created"] == 1  # replayed from the pin
    assert reflush["result"]["flushed"] == 1  # replayed from the pin
    assert reflush["result"].get("rejected", 0) == 0
    assert reflush["result"].get("rejectedCards", 0) == 0
    assert reflush["remainingCards"] == 0
    assert reflush["remainingReviews"] == 0
    assert reflush["rejects"] == 0

    # Zero dupes server-side: still one question, one review, the same
    # FSRS card state, and exactly the two idempotency pins.
    conn = sqlite3.connect(offline_server.db_path)
    try:
        n_questions = conn.execute(
            "SELECT COUNT(*) FROM questions WHERE prompt = ?", (AUTHOR_FRONT,)
        ).fetchone()[0]
        n_reviews = conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE question_id = ?", (qid,)
        ).fetchone()[0]
        next_due_after = conn.execute(
            "SELECT next_due FROM cards WHERE question_id = ?", (qid,)
        ).fetchone()[0]
        n_pins = conn.execute(
            "SELECT COUNT(*) FROM offline_sync_idempotency WHERE question_id = ?", (qid,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert n_questions == 1
    assert n_reviews == 1
    assert next_due_after == next_due  # the replay never re-ran the scheduler
    assert n_pins == 2


# ---- the partial-flush trap, M4 edition -------------------------------
#
# The M3 study suite pins that a forced snapshot refresh after a
# PARTIAL flush preserves ladder overlays for snapshot cards with
# queued reviews. The M4 twin: an offline-AUTHORED card whose creation
# acked but whose review chunk failed transiently. Its overlay lived
# on the local_cards row (deleted on ack) and its queued review is
# keyed by card_client_id, so the refresh must carry the overlay onto
# the newly delivered snapshot card or the just-studied card
# resurfaces as due.

_PARTIAL_FLUSH_JS = """
async ({prefix, front, back}) => {
  const store = await import(prefix + "offline/store.js");
  const sync = await import(prefix + "offline/sync.js");
  const cardClientId = store.uuid();
  const futureDue = new Date(Date.now() + 86400000).toISOString();
  // The exact rows the authoring form + a self-verdict would write.
  await store.put("local_cards", {
    client_id: cardClientId,
    deck_id: null,
    prompt: front,
    answer: back,
    created_at: new Date().toISOString(),
    local_step: 1,
    local_next_due: futureDue,
  });
  await store.put("outbox_reviews", {
    client_id: store.uuid(),
    card_client_id: cardClientId,
    verdict: "right",
    user_answer: "studied offline",
    graded_by: "self",
    reviewed_at: new Date().toISOString(),
  });
  const flush = await sync.flushOutbox();  // review chunk blocked by the test route
  const refreshed = await sync.refreshSnapshot({force: true});
  const cards = await store.getAll("cards");
  const authored = cards.find((c) => c.prompt === front);
  return {
    flush,
    refreshOk: Boolean(refreshed && refreshed.ok),
    localCardsLeft: (await store.getAll("local_cards")).length,
    outboxLeft: (await store.getAll("outbox_reviews")).length,
    authored: authored
      ? {step: authored.local_step ?? null, due: authored.local_next_due ?? null}
      : null,
    futureDue,
  };
}
"""


def test_partial_flush_preserves_authored_card_overlay(
    offline_server, offline_ctx, offline_page, authored_rows_purged
):
    """Card chunk acks, review chunk fails transiently (simulated by
    aborting sync POSTs whose new_cards is empty), then the forced
    refresh runs: the authored card must arrive in the snapshot WITH
    its ladder overlay while its card_client_id review is still
    queued, so it does not resurface in the due queue."""
    offline_server.start()  # idempotent; heals a prior test's failure state
    page = offline_page

    def _block_review_chunks(route):
        body = route.request.post_data or ""
        if '"new_cards":[]' in body:
            route.abort()
        else:
            route.fallback()  # card chunks fall through to header injection

    offline_ctx.route("**/api/offline/sync", _block_review_chunks)

    # Online module drive: no SW priming needed, the shell's importmap
    # is enough to import the real modules.
    page.goto(offline_server.base_url + "/offline")
    result = page.evaluate(
        _PARTIAL_FLUSH_JS,
        {"prefix": _module_prefix(page), "front": AUTHOR_FRONT, "back": AUTHOR_BACK},
    )
    assert result["flush"]["created"] == 1, result
    assert result["flush"]["flushed"] == 0, result
    assert result["refreshOk"] is True, result
    assert result["localCardsLeft"] == 0  # the card itself was acked
    assert result["outboxLeft"] == 1  # the review is still queued
    assert result["authored"] is not None, "snapshot refresh should deliver the created card"
    # The pin: the overlay survived the refresh, so the studied card
    # stays scheduled instead of resurfacing as due.
    assert result["authored"] == {"step": 1, "due": result["futureDue"]}


# ---- the caught-up entry point ----------------------------------------


def _reset_seed_due_times(offline_server) -> None:
    """Restore the seeded cards' next_due to the canonical
    staggered-past values so the due queue is deterministic regardless
    of which sibling suite ran first in this session (the study suite
    leaves the seeded cards rescheduled into the future)."""
    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(offline_server.db_path)
    try:
        for key, hours in (("mcq_id", 3), ("regex_id", 2), ("short_id", 1)):
            conn.execute(
                "UPDATE cards SET next_due = ? WHERE question_id = ?",
                ((now - timedelta(hours=hours)).isoformat(), offline_server.seed[key]),
            )
        conn.commit()
    finally:
        conn.close()


def test_caught_up_view_offers_authoring(offline_server, offline_ctx, offline_page):
    """The caught-up view's primary action is Add a card (docs section
    2: 'when the queue is empty ... plus the Add-a-card action').
    Reached genuinely: every due card is answered with 'I don't know'
    offline. Nothing here ever syncs (the server stays stopped from
    the offline transition to the end), so the queued wrong verdicts
    die with the function-scoped browser context and the server db is
    untouched."""
    base = offline_server.base_url
    page = offline_page
    offline_server.start()  # idempotent; heals a prior test's failure state
    _reset_seed_due_times(offline_server)
    _prime_online(page, base)
    offline_server.stop()
    try:
        offline_ctx.set_offline(True)
        page.goto(base + "/")
        page.wait_for_selector("[data-offline-root] .prelude")
        due = _due_count(page)
        assert due == 3

        # Drain the queue: idk marks each card wrong (+10 min), so the
        # queue shrinks by one per card until caught up.
        page.get_by_role("button", name="Study").click()
        for _ in range(due):
            page.wait_for_selector(".study-card")
            page.get_by_role("button", name="I don't know").click()
            page.wait_for_selector("h1.verdict-headline")
            page.get_by_role("button", name="Next card").click()
        page.wait_for_selector(".empty-state")

        # The caught-up view's PRIMARY action is authoring.
        add = page.locator(".caughtup-actions .btn-primary")
        assert add.inner_text() == "Add a card"
        add.click()
        page.wait_for_selector(".author-form")
        page.locator("button.back").click()
        page.wait_for_selector(".offline-due")
        assert _idb_all(page, "local_cards") == []
        assert len(_idb_all(page, "outbox_reviews")) == 3  # never synced
    finally:
        # Sibling suites (parity) navigate straight to the live
        # server; never leave it stopped.
        offline_server.start()
