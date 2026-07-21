"""Route + service tests for POST /api/offline/sync (docs/OFFLINE.md
sections 4 and 7, M2 scope).

The contract pinned here: auth required, per-item rejection (a bad
item never 4xxs the batch), cards-before-reviews, timestamp-ordered
FSRS replay through the real scheduler, last-writer-wins with the
logged_no_reschedule audit marker, clock clamping, idempotent
re-POSTs, and batch caps.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from prep.decks.entities import NewQuestion, QuestionType
from prep.decks.repo import DeckRepo, QuestionRepo
from prep.domain.srs import CardSRSState, Verdict, schedule_review
from prep.infrastructure.db import cursor

SYNC_URL = "/api/offline/sync"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _review(client_id: str, qid: int, reviewed_at: str, **overrides) -> dict:
    item = {
        "client_id": client_id,
        "question_id": qid,
        "verdict": "right",
        "user_answer": "Lima",
        "graded_by": "auto",
        "reviewed_at": reviewed_at,
    }
    item.update(overrides)
    return item


def _card_row(qid: int) -> dict:
    with cursor() as c:
        return dict(c.execute("SELECT * FROM cards WHERE question_id = ?", (qid,)).fetchone())


def _review_rows(qid: int) -> list[dict]:
    with cursor() as c:
        rows = c.execute(
            "SELECT * FROM reviews WHERE question_id = ? ORDER BY id", (qid,)
        ).fetchall()
    return [dict(r) for r in rows]


@pytest.fixture
def sync_seed(initialized_db: str) -> dict:
    """One SRS deck with a short card for the default test user."""
    user = initialized_db
    deck_id = DeckRepo().create(user, "capitals", display_name="Capitals")
    qid = QuestionRepo().add(
        user,
        deck_id,
        NewQuestion(type=QuestionType.SHORT, prompt="Capital of Peru?", answer="Lima"),
    )
    return {"user": user, "deck_id": deck_id, "qid": qid}


def test_sync_requires_auth(unauthed_client: TestClient):
    r = unauthed_client.post(SYNC_URL, json={"reviews": []})
    assert r.status_code == 401


def test_empty_batch_is_a_no_op(client: TestClient, sync_seed: dict):
    r = client.post(SYNC_URL, json={})
    assert r.status_code == 200
    assert r.json() == {"cards": [], "reviews": []}


def test_applied_review_runs_the_real_scheduler(client: TestClient, sync_seed: dict):
    """A queued review replays through schedule_review at
    now=reviewed_at: the audit row carries the client timestamp + the
    offline grader marker, and the card's FSRS state advances."""
    qid = sync_seed["qid"]
    t = _now() - timedelta(hours=1)
    r = client.post(SYNC_URL, json={"reviews": [_review("r1", qid, t.isoformat())]})
    assert r.status_code == 200
    assert r.json()["reviews"] == [{"client_id": "r1", "status": "applied"}]

    card = _card_row(qid)
    assert card["last_review"] == t.isoformat()
    assert card["stability"] is not None

    rows = _review_rows(qid)
    assert len(rows) == 1
    assert rows[0]["ts"] == t.isoformat()
    assert rows[0]["result"] == "right"
    assert rows[0]["grader_notes"] == "(offline auto)"


def test_replay_is_timestamp_ordered_and_matches_direct_scheduling(
    client: TestClient, sync_seed: dict
):
    """Reviews shuffled in the request replay in reviewed_at order,
    and the resulting card state equals calling schedule_review
    directly with those timestamps -- the service adds no scheduling
    logic of its own."""
    qid = sync_seed["qid"]
    t1 = _now() - timedelta(days=3)
    t2 = t1 + timedelta(days=1)
    t3 = t1 + timedelta(days=2)

    # Request order deliberately scrambled: t3, t1, t2.
    r = client.post(
        SYNC_URL,
        json={
            "reviews": [
                _review("r3", qid, t3.isoformat(), verdict="right"),
                _review("r1", qid, t1.isoformat(), verdict="right"),
                _review("r2", qid, t2.isoformat(), verdict="wrong"),
            ]
        },
    )
    assert r.status_code == 200
    assert [item["status"] for item in r.json()["reviews"]] == ["applied"] * 3

    s1 = schedule_review(CardSRSState.fresh(), Verdict.RIGHT, now=t1)
    s2 = schedule_review(s1.state, Verdict.WRONG, now=t2)
    s3 = schedule_review(s2.state, Verdict.RIGHT, now=t3)

    card = _card_row(qid)
    assert card["stability"] == pytest.approx(s3.state.stability)
    assert card["difficulty"] == pytest.approx(s3.state.difficulty)
    assert card["fsrs_state"] == s3.state.fsrs_state
    assert card["next_due"] == s3.next_due.isoformat()
    assert card["last_review"] == t3.isoformat()

    # The audit log holds all three, in replay order.
    assert [row["ts"] for row in _review_rows(qid)] == [
        t1.isoformat(),
        t2.isoformat(),
        t3.isoformat(),
    ]


def test_superseded_review_logs_without_rescheduling(client: TestClient, sync_seed: dict):
    """Last-writer-wins: a review older than the card's last_review
    (another device already studied it) lands in the audit log but
    never touches card state."""
    from prep.study.repo import ReviewRepo

    user, qid = sync_seed["user"], sync_seed["qid"]
    ReviewRepo().record(user, qid, "right", "Lima")
    card_before = _card_row(qid)

    stale = _now() - timedelta(days=1)
    r = client.post(SYNC_URL, json={"reviews": [_review("r1", qid, stale.isoformat())]})
    assert r.status_code == 200
    assert r.json()["reviews"] == [{"client_id": "r1", "status": "logged_no_reschedule"}]

    assert _card_row(qid) == card_before
    rows = _review_rows(qid)
    assert len(rows) == 2  # the online review + the offline audit row
    assert rows[1]["ts"] == stale.isoformat()


def test_future_reviewed_at_is_clamped_to_server_now(client: TestClient, sync_seed: dict):
    qid = sync_seed["qid"]
    future = _now() + timedelta(days=3)
    r = client.post(SYNC_URL, json={"reviews": [_review("r1", qid, future.isoformat())]})
    assert r.status_code == 200
    assert r.json()["reviews"][0]["status"] == "applied"

    card = _card_row(qid)
    applied_at = datetime.fromisoformat(card["last_review"])
    assert applied_at <= _now()

    rows = _review_rows(qid)
    assert "clamped" in rows[0]["grader_notes"]
    assert future.isoformat() in rows[0]["grader_notes"]


def test_bad_items_reject_without_failing_the_batch(client: TestClient, sync_seed: dict):
    """Per-item savepoints: every malformed item comes back rejected
    with its error, while the one good item in the same batch still
    applies."""
    qid = sync_seed["qid"]
    good_t = _now() - timedelta(minutes=5)
    naive = _now().replace(tzinfo=None)
    r = client.post(
        SYNC_URL,
        json={
            "reviews": [
                _review("r1", qid, naive.isoformat()),
                _review("r2", qid, "not-a-timestamp"),
                _review("r3", qid, good_t.isoformat(), verdict="maybe"),
                _review("r4", qid, good_t.isoformat(), graded_by="claude"),
                _review("r5", qid, good_t.isoformat(), question_id=None),
                _review("r6", qid, good_t.isoformat(), card_client_id="also-set"),
                _review("r7", None, good_t.isoformat(), question_id=None, card_client_id="ghost"),
                {
                    # no client_id at all -- without one the item's
                    # fate cannot be pinned, so it rejects.
                    "question_id": qid,
                    "verdict": "right",
                    "user_answer": "",
                    "graded_by": "auto",
                    "reviewed_at": good_t.isoformat(),
                },
                _review("r9", qid, good_t.isoformat()),
            ]
        },
    )
    assert r.status_code == 200
    results = r.json()["reviews"]
    by_error = {item.get("client_id"): item for item in results}

    assert "offset" in by_error["r1"]["error"]
    assert by_error["r2"]["error"] == "reviewed_at is not ISO-8601"
    assert by_error["r3"]["error"] == "unknown verdict"
    assert by_error["r4"]["error"] == "unknown graded_by"
    assert by_error["r5"]["error"] == "question_id or card_client_id required"
    assert by_error["r6"]["error"] == "give question_id or card_client_id, not both"
    assert by_error["r7"]["error"] == "unknown card_client_id"
    for cid in ("r1", "r2", "r3", "r4", "r5", "r6", "r7"):
        assert by_error[cid]["status"] == "rejected"
    missing_id = next(item for item in results if "client_id" not in item)
    assert missing_id == {"status": "rejected", "error": "client_id required"}

    assert by_error["r9"] == {"client_id": "r9", "status": "applied"}
    assert len(_review_rows(qid)) == 1


def test_unknown_and_foreign_question_ids_reject_identically(client: TestClient, sync_seed: dict):
    """IDOR discipline: another user's question id is exactly as
    invisible as a nonexistent one, and neither writes anything."""
    from prep.auth.repo import UserRepo

    other = "bob@example.com"
    UserRepo().upsert(other, display_name="Bob")
    bob_deck = DeckRepo().create(other, "bobs-deck")
    bob_q = QuestionRepo().add(
        other,
        bob_deck,
        NewQuestion(type=QuestionType.SHORT, prompt="bob's card", answer="secret"),
    )

    t = _now().isoformat()
    r = client.post(
        SYNC_URL,
        json={"reviews": [_review("r1", bob_q, t), _review("r2", 999999, t)]},
    )
    assert r.status_code == 200
    for item in r.json()["reviews"]:
        assert item["status"] == "rejected"
        assert item["error"] == "unknown question_id"
    assert _review_rows(bob_q) == []


def test_retried_batch_replays_as_pure_lookups(client: TestClient, sync_seed: dict):
    """Idempotency: re-POSTing the identical batch returns the
    identical response with zero new rows and unchanged FSRS state."""
    qid = sync_seed["qid"]
    t = (_now() - timedelta(hours=2)).isoformat()
    batch = {
        "new_cards": [
            {
                "client_id": "c1",
                "deck_id": sync_seed["deck_id"],
                "prompt": "front",
                "answer": "back",
            }
        ],
        "reviews": [_review("r1", qid, t)],
    }

    first = client.post(SYNC_URL, json=batch)
    assert first.status_code == 200
    assert first.json()["cards"][0]["status"] == "created"
    card_state = _card_row(qid)
    with cursor() as c:
        question_count = c.execute("SELECT COUNT(*) AS n FROM questions").fetchone()["n"]

    second = client.post(SYNC_URL, json=batch)
    assert second.status_code == 200
    assert second.json() == first.json()
    assert _card_row(qid) == card_state
    assert len(_review_rows(qid)) == 1
    with cursor() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM questions").fetchone()["n"] == question_count


def test_new_card_paths(client: TestClient, sync_seed: dict):
    """Card ingestion: explicit deck, the inbox fallback for a null
    deck_id, rejection for unknown/foreign decks, and the required
    prompt/answer validation."""
    user = sync_seed["user"]
    other = "bob@example.com"
    from prep.auth.repo import UserRepo

    UserRepo().upsert(other, display_name="Bob")
    bob_deck = DeckRepo().create(other, "bobs-deck")

    r = client.post(
        SYNC_URL,
        json={
            "new_cards": [
                {
                    "client_id": "c1",
                    "deck_id": sync_seed["deck_id"],
                    "prompt": "f1",
                    "answer": "b1",
                },
                {"client_id": "c2", "prompt": "f2", "answer": "b2"},
                {"client_id": "c3", "deck_id": 999999, "prompt": "f3", "answer": "b3"},
                {"client_id": "c4", "deck_id": bob_deck, "prompt": "f4", "answer": "b4"},
                {"client_id": "c5", "prompt": "   ", "answer": "b5"},
                {"client_id": "c6", "prompt": "f6", "answer": ""},
            ]
        },
    )
    assert r.status_code == 200
    results = {item["client_id"]: item for item in r.json()["cards"]}

    # Created in the named deck, type short, due immediately.
    assert results["c1"]["status"] == "created"
    qid1 = results["c1"]["question_id"]
    q1 = QuestionRepo().get(user, qid1)
    assert q1 is not None and q1.type.value == "short" and q1.deck_id == sync_seed["deck_id"]
    card1 = _card_row(qid1)
    assert datetime.fromisoformat(card1["next_due"]) <= _now()

    # Null deck_id files into the get-or-created inbox deck.
    assert results["c2"]["status"] == "created"
    inbox_id = DeckRepo().find_id(user, "inbox")
    assert inbox_id is not None
    q2 = QuestionRepo().get(user, results["c2"]["question_id"])
    assert q2 is not None and q2.deck_id == inbox_id

    # Unknown deck and another user's deck reject with the same error.
    assert results["c3"] == {"client_id": "c3", "status": "rejected", "error": "unknown deck_id"}
    assert results["c4"] == {"client_id": "c4", "status": "rejected", "error": "unknown deck_id"}

    assert results["c5"]["error"] == "prompt required"
    assert results["c6"]["error"] == "answer required"


def test_cards_process_before_reviews_within_a_batch(client: TestClient, sync_seed: dict):
    """A review referencing a card created in the SAME batch resolves
    through the idempotency mapping, even when its reviewed_at
    predates the card's server-side creation time."""
    t = _now() - timedelta(days=1)
    r = client.post(
        SYNC_URL,
        json={
            "new_cards": [{"client_id": "c1", "prompt": "front", "answer": "back"}],
            "reviews": [
                {
                    "client_id": "r1",
                    "card_client_id": "c1",
                    "verdict": "wrong",
                    "user_answer": "",
                    "graded_by": "self",
                    "reviewed_at": t.isoformat(),
                }
            ],
        },
    )
    assert r.status_code == 200
    payload = r.json()
    qid = payload["cards"][0]["question_id"]
    assert payload["reviews"] == [{"client_id": "r1", "status": "applied"}]

    rows = _review_rows(qid)
    assert len(rows) == 1
    assert rows[0]["result"] == "wrong"
    assert rows[0]["grader_notes"] == "(offline self-graded)"
    assert _card_row(qid)["last_review"] == t.isoformat()


def test_batch_caps_are_enforced(client: TestClient, sync_seed: dict):
    """Over-cap batches are a protocol violation (the client chunks
    under 100 cards / 500 reviews), rejected at the parse layer."""
    t = _now().isoformat()
    too_many = [_review(f"r{i}", sync_seed["qid"], t) for i in range(501)]
    assert client.post(SYNC_URL, json={"reviews": too_many}).status_code == 422

    cards = [{"client_id": f"c{i}", "prompt": "f", "answer": "b"} for i in range(101)]
    assert client.post(SYNC_URL, json={"new_cards": cards}).status_code == 422


def test_interleaved_new_card_and_existing_card_reviews_replay_globally_ordered(
    client: TestClient, sync_seed: dict
):
    """The full interleave from the spec's unit list: one batch mixes
    a new card with reviews that alternate between that card and an
    existing question, scrambled in the request. Replay must order by
    reviewed_at across the WHOLE batch (observable via the append-only
    log's insertion order across both questions), and each card's end
    state must equal direct schedule_review calls over its own
    timestamps."""
    qid = sync_seed["qid"]
    t0 = _now() - timedelta(days=2)
    tq1, tc1, tq2, tc2 = (t0 + timedelta(hours=h) for h in (0, 1, 2, 3))

    r = client.post(
        SYNC_URL,
        json={
            "new_cards": [{"client_id": "c1", "prompt": "front", "answer": "back"}],
            "reviews": [
                # Request order scrambled: tc2, tq1, tc1, tq2.
                {
                    "client_id": "rc2",
                    "card_client_id": "c1",
                    "verdict": "right",
                    "user_answer": "",
                    "graded_by": "self",
                    "reviewed_at": tc2.isoformat(),
                },
                _review("rq1", qid, tq1.isoformat(), verdict="right"),
                {
                    "client_id": "rc1",
                    "card_client_id": "c1",
                    "verdict": "wrong",
                    "user_answer": "",
                    "graded_by": "self",
                    "reviewed_at": tc1.isoformat(),
                },
                _review("rq2", qid, tq2.isoformat(), verdict="wrong"),
            ],
        },
    )
    assert r.status_code == 200
    payload = r.json()
    new_qid = payload["cards"][0]["question_id"]
    assert {item["status"] for item in payload["reviews"]} == {"applied"}

    # Global replay order: the audit rows landed tq1, tc1, tq2, tc2
    # across the two questions (insertion id is the order witness).
    with cursor() as c:
        rows = c.execute(
            "SELECT question_id, ts FROM reviews ORDER BY id",
        ).fetchall()
    assert [(row["question_id"], row["ts"]) for row in rows] == [
        (qid, tq1.isoformat()),
        (new_qid, tc1.isoformat()),
        (qid, tq2.isoformat()),
        (new_qid, tc2.isoformat()),
    ]

    # Per-card parity with direct scheduling over each subsequence.
    sq1 = schedule_review(CardSRSState.fresh(), Verdict.RIGHT, now=tq1)
    sq2 = schedule_review(sq1.state, Verdict.WRONG, now=tq2)
    card_q = _card_row(qid)
    assert card_q["stability"] == pytest.approx(sq2.state.stability)
    assert card_q["next_due"] == sq2.next_due.isoformat()

    sc1 = schedule_review(CardSRSState.fresh(), Verdict.WRONG, now=tc1)
    sc2 = schedule_review(sc1.state, Verdict.RIGHT, now=tc2)
    card_c = _card_row(new_qid)
    assert card_c["stability"] == pytest.approx(sc2.state.stability)
    assert card_c["next_due"] == sc2.next_due.isoformat()


def test_deck_retention_override_flows_into_replay(client: TestClient, sync_seed: dict):
    """Replay-math parity includes the retention seam: a deck-level
    desired_retention override must reach schedule_review exactly as
    the online record() path passes it. The card is seeded into FSRS
    Review state (fsrs_state=2) because retention only shapes the
    interval there; learning-phase steps are fixed."""
    qid, deck_id = sync_seed["qid"], sync_seed["deck_id"]
    t0 = _now() - timedelta(days=5)
    seeded = CardSRSState(stability=10.0, difficulty=5.0, fsrs_state=2, last_review=t0)
    with cursor() as c:
        c.execute("UPDATE decks SET desired_retention = 0.95 WHERE id = ?", (deck_id,))
        c.execute(
            "UPDATE cards SET stability = ?, difficulty = ?, fsrs_state = ?, last_review = ? "
            " WHERE question_id = ?",
            (seeded.stability, seeded.difficulty, seeded.fsrs_state, t0.isoformat(), qid),
        )

    t1 = t0 + timedelta(days=2)
    r = client.post(SYNC_URL, json={"reviews": [_review("r1", qid, t1.isoformat())]})
    assert r.status_code == 200
    assert r.json()["reviews"] == [{"client_id": "r1", "status": "applied"}]

    # FSRS interval fuzzing randomizes day-scale intervals a little on
    # every call, so exact next_due equality cannot be asserted here
    # (the Learning-state parity tests above are exact because fixed
    # learning steps are not fuzzed). Stability is fuzz-free; for the
    # interval, assert the replay landed on the override's schedule,
    # not the default one, with a guard that the two are far apart.
    expected = schedule_review(seeded, Verdict.RIGHT, now=t1, desired_retention=0.95)
    default = schedule_review(seeded, Verdict.RIGHT, now=t1)
    d95 = (expected.next_due - t1).total_seconds()
    ddef = (default.next_due - t1).total_seconds()
    assert ddef - d95 > 4 * 86400  # non-vacuous: the override separates the schedules

    card = _card_row(qid)
    assert card["stability"] == pytest.approx(expected.state.stability)
    dcard = (datetime.fromisoformat(card["next_due"]) - t1).total_seconds()
    assert abs(dcard - d95) < abs(dcard - ddef)


def test_duplicate_client_ids_within_one_batch_replay_not_500(client: TestClient, sync_seed: dict):
    """Two reviews sharing a client_id in the SAME batch: the earliest
    applies, the duplicate replays that pinned outcome (same rule as a
    retried batch), and the batch never 4xxs or 5xxs. Only one review
    row lands and the card state equals a single direct call."""
    qid = sync_seed["qid"]
    t1 = _now() - timedelta(hours=2)
    t2 = t1 + timedelta(hours=1)
    r = client.post(
        SYNC_URL,
        json={
            "reviews": [
                _review("dup", qid, t1.isoformat(), verdict="right"),
                _review("dup", qid, t2.isoformat(), verdict="wrong"),
            ]
        },
    )
    assert r.status_code == 200
    assert r.json()["reviews"] == [
        {"client_id": "dup", "status": "applied"},
        {"client_id": "dup", "status": "applied"},
    ]

    rows = _review_rows(qid)
    assert len(rows) == 1
    assert rows[0]["ts"] == t1.isoformat()
    direct = schedule_review(CardSRSState.fresh(), Verdict.RIGHT, now=t1)
    card = _card_row(qid)
    assert card["stability"] == pytest.approx(direct.state.stability)
    assert card["last_review"] == t1.isoformat()


def test_type_malformed_items_reject_per_item_not_batch(client: TestClient, sync_seed: dict):
    """The loose-typing half of per-item isolation: a field of the
    wrong JSON type (a corrupt outbox row) must land that ITEM in
    rejected, never 422 the batch -- a parse-layer 422 would wedge the
    whole outbox forever. The good items in the same batch still
    apply."""
    qid = sync_seed["qid"]
    t = (_now() - timedelta(minutes=10)).isoformat()
    r = client.post(
        SYNC_URL,
        json={
            "device_id": 123,  # bookkeeping only; tolerated
            "new_cards": [
                {"client_id": "cbad1", "prompt": 42, "answer": "b"},
                {"client_id": "cbad2", "deck_id": "three", "prompt": "f", "answer": "b"},
                {"client_id": "cgood", "prompt": "f", "answer": "b"},
            ],
            "reviews": [
                _review("rbad1", "abc", t),
                {**_review("", qid, t), "client_id": 12345},
                _review("rbad3", qid, t, graded_by={"x": 1}),
                {**_review("rbad4", qid, t), "reviewed_at": ["2030-01-01"]},
                _review("rbad5", None, t, question_id=None, card_client_id={"nested": True}),
                _review("rgood", qid, t),
            ],
        },
    )
    assert r.status_code == 200
    payload = r.json()

    cards = {item.get("client_id"): item for item in payload["cards"]}
    assert cards["cbad1"]["status"] == "rejected"
    assert cards["cbad2"] == {
        "client_id": "cbad2",
        "status": "rejected",
        "error": "unknown deck_id",
    }
    assert cards["cgood"]["status"] == "created"

    reviews = {item.get("client_id"): item for item in payload["reviews"]}
    assert reviews["rbad1"] == {
        "client_id": "rbad1",
        "status": "rejected",
        "error": "unknown question_id",
    }
    # A non-string client_id cannot be echoed (or correlated); the
    # reject comes back id-less, same shape as a missing client_id.
    assert reviews[None] == {"status": "rejected", "error": "client_id required"}
    assert reviews["rbad3"] == {
        "client_id": "rbad3",
        "status": "rejected",
        "error": "unknown graded_by",
    }
    assert reviews["rbad4"]["status"] == "rejected"
    assert "ISO-8601" in reviews["rbad4"]["error"]
    assert reviews["rbad5"] == {
        "client_id": "rbad5",
        "status": "rejected",
        "error": "unknown card_client_id",
    }
    assert reviews["rgood"] == {"client_id": "rgood", "status": "applied"}
    assert len(_review_rows(qid)) == 1


def test_equal_timestamps_within_batch_are_last_writer_resolved(
    client: TestClient, sync_seed: dict
):
    """Two reviews of the same card at the identical instant: request
    position breaks the ordering tie, so the first applies and the
    second hits the <= last_review rule -- logged_no_reschedule, not a
    second scheduler run."""
    qid = sync_seed["qid"]
    t = _now() - timedelta(hours=1)
    r = client.post(
        SYNC_URL,
        json={
            "reviews": [
                _review("r1", qid, t.isoformat(), verdict="right"),
                _review("r2", qid, t.isoformat(), verdict="wrong"),
            ]
        },
    )
    assert r.status_code == 200
    assert r.json()["reviews"] == [
        {"client_id": "r1", "status": "applied"},
        {"client_id": "r2", "status": "logged_no_reschedule"},
    ]

    direct = schedule_review(CardSRSState.fresh(), Verdict.RIGHT, now=t)
    card = _card_row(qid)
    assert card["stability"] == pytest.approx(direct.state.stability)
    assert len(_review_rows(qid)) == 2


def test_logged_no_reschedule_retry_replays_same_status(client: TestClient, sync_seed: dict):
    """Idempotency covers the conflict outcome too: a superseded
    review re-POSTed comes back logged_no_reschedule again with zero
    new audit rows."""
    from prep.study.repo import ReviewRepo

    user, qid = sync_seed["user"], sync_seed["qid"]
    ReviewRepo().record(user, qid, "right", "Lima")

    stale = (_now() - timedelta(days=1)).isoformat()
    batch = {"reviews": [_review("r1", qid, stale)]}
    first = client.post(SYNC_URL, json=batch)
    assert first.json()["reviews"][0]["status"] == "logged_no_reschedule"
    card_state = _card_row(qid)

    second = client.post(SYNC_URL, json=batch)
    assert second.status_code == 200
    assert second.json() == first.json()
    assert _card_row(qid) == card_state
    assert len(_review_rows(qid)) == 2  # online row + ONE offline audit row


def test_client_id_reuse_across_kinds_rejects(client: TestClient, sync_seed: dict):
    """A client_id pins one item forever: reusing a card's UUID for a
    review (or a review's for a card) in a later batch rejects instead
    of replaying the wrong kind's outcome."""
    qid = sync_seed["qid"]
    t = _now().isoformat()
    first = client.post(
        SYNC_URL,
        json={
            "new_cards": [{"client_id": "card-id", "prompt": "f", "answer": "b"}],
            "reviews": [_review("review-id", qid, t)],
        },
    )
    assert first.status_code == 200

    second = client.post(
        SYNC_URL,
        json={
            "new_cards": [{"client_id": "review-id", "prompt": "f2", "answer": "b2"}],
            "reviews": [_review("card-id", qid, t)],
        },
    )
    assert second.status_code == 200
    payload = second.json()
    assert payload["cards"][0] == {
        "client_id": "review-id",
        "status": "rejected",
        "error": "client_id already used by a review item",
    }
    assert payload["reviews"][0] == {
        "client_id": "card-id",
        "status": "rejected",
        "error": "client_id already used by a card item",
    }


def test_review_of_a_rejected_card_in_the_same_batch_rejects(client: TestClient, sync_seed: dict):
    """When a card item fails validation, a review targeting its
    card_client_id cannot resolve: the review rejects as unknown
    card_client_id (client-side, the queued-card rule keeps it in the
    outbox for the retry after the card is fixed)."""
    t = _now().isoformat()
    r = client.post(
        SYNC_URL,
        json={
            "new_cards": [{"client_id": "c1", "prompt": "front", "answer": ""}],
            "reviews": [
                {
                    "client_id": "r1",
                    "card_client_id": "c1",
                    "verdict": "right",
                    "user_answer": "",
                    "graded_by": "self",
                    "reviewed_at": t,
                }
            ],
        },
    )
    assert r.status_code == 200
    payload = r.json()
    assert payload["cards"][0]["status"] == "rejected"
    assert payload["reviews"][0] == {
        "client_id": "r1",
        "status": "rejected",
        "error": "unknown card_client_id",
    }
