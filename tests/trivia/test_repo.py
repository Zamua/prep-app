"""Integration tests for prep.trivia.repo.

The fixtures in conftest.py spin up an isolated sqlite per test +
upsert a default user. We seed a trivia deck and a few questions
directly via the existing decks repo, then exercise queue ops.
"""

from __future__ import annotations

import pytest

from prep.decks.entities import NewQuestion, QuestionType
from prep.decks.repo import DeckRepo, QuestionRepo
from prep.trivia.repo import TriviaQueueRepo, TriviaSessionsRepo


@pytest.fixture
def repos(initialized_db: str):
    return {
        "user": initialized_db,
        "decks": DeckRepo(),
        "questions": QuestionRepo(),
        "trivia": TriviaQueueRepo(),
    }


def _seed_trivia_deck(repos, name="capitals", n_questions=3):
    """Helper: create a deck + N questions + queue entries."""
    user = repos["user"]
    deck_id = repos["decks"].create(user, name)
    qids = []
    for i in range(n_questions):
        qid = repos["questions"].add(
            user,
            deck_id,
            NewQuestion(
                type=QuestionType.SHORT,
                topic=name,
                prompt=f"What is the capital of country #{i}?",
                answer=f"Capital{i}",
            ),
        )
        repos["trivia"].append_card(qid, deck_id)
        qids.append(qid)
    return deck_id, qids


# ---- append_card -------------------------------------------------------


def test_append_card_assigns_monotonic_positions(repos):
    """Positions still climb with insertion order (archive export +
    import round-trip depend on it). The PICKER no longer follows that
    order: within a tier it picks at random: so this asserts the
    stored positions, not which card comes out."""
    from prep.infrastructure.db import cursor

    deck_id, qids = _seed_trivia_deck(repos, n_questions=3)
    with cursor() as c:
        positions = [
            c.execute(
                "SELECT queue_position FROM trivia_queue WHERE question_id = ?", (qid,)
            ).fetchone()["queue_position"]
            for qid in qids
        ]
    assert positions == sorted(positions)
    assert len(set(positions)) == 3
    # And the deck is pickable: some fresh card from this deck comes out.
    nxt = repos["trivia"].pick_next_for_deck(deck_id)
    assert nxt is not None
    assert nxt.question_id in qids
    assert nxt.is_fresh is True


def test_append_card_isolates_per_deck(repos):
    """Two decks get independent queue numbering — appending to deck B
    doesn't bump positions in deck A."""
    deck_a, _ = _seed_trivia_deck(repos, "deck-a", n_questions=2)
    deck_b, _ = _seed_trivia_deck(repos, "deck-b", n_questions=2)
    a_next = repos["trivia"].pick_next_for_deck(deck_a)
    b_next = repos["trivia"].pick_next_for_deck(deck_b)
    assert a_next.deck_id == deck_a
    assert b_next.deck_id == deck_b


# ---- pick_next_for_deck ------------------------------------------------


def test_pick_next_returns_none_for_empty_deck(repos):
    user = repos["user"]
    deck_id = repos["decks"].create(user, "empty")
    assert repos["trivia"].pick_next_for_deck(deck_id) is None


def test_pick_next_prefers_unanswered_over_answered(repos):
    """After we mark card #0 answered, the picker should skip it and
    return one of the never-answered cards. WHICH never-answered card
    is deliberately random (same tier), so the assertion is on the
    tier, not the id."""
    deck_id, qids = _seed_trivia_deck(repos, n_questions=3)
    trivia = repos["trivia"]
    trivia.mark_answered(qids[0], correct=True)
    nxt = trivia.pick_next_for_deck(deck_id)
    assert nxt.question_id in {qids[1], qids[2]}
    assert nxt.is_fresh is True


def test_pick_next_prefers_wrong_over_fresh(repos):
    """A wrong-answered card outranks a never-shown one — we want
    the user to see the miss again before getting more new content."""
    deck_id, qids = _seed_trivia_deck(repos, n_questions=4)
    trivia = repos["trivia"]
    # Take one card off the front, mark it wrong. qids[1..3] are still fresh.
    trivia.mark_answered(qids[0], correct=False)
    nxt = trivia.pick_next_for_deck(deck_id)
    assert nxt.question_id == qids[0]
    assert nxt.is_fresh is False


def test_pick_next_correct_card_outranked_by_fresh(repos):
    """A correctly-answered card should NOT shadow fresh content —
    correctness drops a card to the bottom of the precedence list."""
    deck_id, qids = _seed_trivia_deck(repos, n_questions=4)
    trivia = repos["trivia"]
    trivia.mark_answered(qids[0], correct=True)
    nxt = trivia.pick_next_for_deck(deck_id)
    # qids[1..3] all rank "fresh" and are equally likely; the correct
    # card must not appear at all while fresh content is left.
    assert nxt.question_id in set(qids[1:])
    assert nxt.is_fresh is True


def test_pick_next_wrong_outranks_correct(repos):
    """When everything's been answered, wrong cards come back ahead
    of right cards regardless of queue_position order."""
    deck_id, qids = _seed_trivia_deck(repos, n_questions=3)
    trivia = repos["trivia"]
    # Mark right first, then wrong → wrong has the higher queue_position
    # but should still surface first because of the rank ordering.
    trivia.mark_answered(qids[0], correct=True)
    trivia.mark_answered(qids[1], correct=False)
    trivia.mark_answered(qids[2], correct=True)
    nxt = trivia.pick_next_for_deck(deck_id)
    assert nxt.question_id == qids[1]


def test_pick_next_falls_back_to_rotated_when_no_unanswered(repos):
    """Once every card's been answered at least once the picker keeps
    working: it returns a rotated (already-seen) card rather than
    None. All three sit in the same tier here, so which one comes back
    is random by design."""
    deck_id, qids = _seed_trivia_deck(repos, n_questions=3)
    trivia = repos["trivia"]
    for qid in qids:
        trivia.mark_answered(qid, correct=True)
    nxt = trivia.pick_next_for_deck(deck_id)
    assert nxt is not None
    assert nxt.question_id in qids
    assert nxt.is_fresh is False


# ---- mark_answered -----------------------------------------------------


def test_mark_answered_rotates_to_back(repos):
    """Answering a card bumps its queue_position to max+1 AND drops it
    out of contention while fresh cards remain."""
    from prep.infrastructure.db import cursor

    deck_id, qids = _seed_trivia_deck(repos, n_questions=3)
    trivia = repos["trivia"]
    trivia.mark_answered(qids[0], correct=True)
    with cursor() as c:
        rows = {
            r["question_id"]: r["queue_position"]
            for r in c.execute("SELECT question_id, queue_position FROM trivia_queue").fetchall()
        }
    assert rows[qids[0]] == max(rows.values())
    # The answered card is not picked again while fresh cards are left.
    assert trivia.pick_next_for_deck(deck_id).question_id != qids[0]


def test_mark_answered_records_verdict(repos):
    deck_id, qids = _seed_trivia_deck(repos, n_questions=2)
    trivia = repos["trivia"]
    trivia.mark_answered(qids[0], correct=True)
    trivia.mark_answered(qids[1], correct=False)
    # count_unanswered should now be 0 — both have last_answered_at set.
    assert trivia.count_unanswered(deck_id) == 0


# ---- pick_session_for_deck ---------------------------------------------


def test_pick_session_default_mix(repos):
    """Brand-new deck of 5: defaults pick 3 = (2 review + 1 fresh).
    Review slots are empty → backfills to 3 fresh cards. Which 3, and
    in what order, is random (all five sit in the same tier)."""
    deck_id, qids = _seed_trivia_deck(repos, n_questions=5)
    session = repos["trivia"].pick_session_for_deck(deck_id)
    ids = [c.question_id for c in session]
    assert len(ids) == 3
    assert len(set(ids)) == 3  # no card served twice in one session
    assert set(ids) <= set(qids)
    assert all(c.is_fresh for c in session)


def test_pick_session_selects_review_before_fresh(repos):
    """Once cards are in the answered pool, the session SELECTS the
    review cards first and spends what's left on fresh content: clear
    debt before reward. This pins membership, not sequence: the order
    the three are shown in is shuffled on purpose."""
    deck_id, qids = _seed_trivia_deck(repos, n_questions=5)
    trivia = repos["trivia"]
    trivia.mark_answered(qids[0], correct=False)  # wrong, in review pool
    trivia.mark_answered(qids[1], correct=True)  # correct, in review pool
    # qids[2..4] are still fresh.
    session = trivia.pick_session_for_deck(deck_id, target_size=3, fresh_target=1)
    ids = {c.question_id for c in session}
    # Both review cards take the two review slots; the last slot is one
    # of the fresh cards.
    assert {qids[0], qids[1]} <= ids
    assert len(ids & set(qids[2:])) == 1
    fresh = [c for c in session if c.is_fresh]
    assert len(fresh) == 1
    assert fresh[0].question_id in set(qids[2:])


def test_pick_session_caps_at_target_size(repos):
    deck_id, qids = _seed_trivia_deck(repos, n_questions=10)
    session = repos["trivia"].pick_session_for_deck(deck_id, target_size=3)
    assert len(session) == 3


def test_pick_session_handles_short_deck(repos):
    deck_id, qids = _seed_trivia_deck(repos, n_questions=2)
    session = repos["trivia"].pick_session_for_deck(deck_id, target_size=3)
    assert len(session) == 2  # can't conjure cards we don't have


def test_pick_session_returns_empty_for_empty_deck(repos):
    user = repos["user"]
    deck_id = repos["decks"].create(user, "empty")
    assert repos["trivia"].pick_session_for_deck(deck_id) == []


# ---- ordering variance -------------------------------------------------
#
# These pin the shuffle itself: the reported bug was that every quiz
# replayed the deck in generation order, so "the order varies" is a
# behavior worth a test, not an implementation detail.
#
# Why they are NOT flaky. Each test builds the same queue N times over
# K cards that all sit in ONE priority tier. A false red needs all N
# builds to agree by chance: roughly (1/K!)^(N-1). With K=5, N=10 that
# is (1/120)^9 ≈ 1e-19: a machine is likelier to fail. So a red here
# means the shuffle is gone, not that we got unlucky: do NOT "fix" it
# by seeding random, dropping the loop, or asserting >= 1 sequence.
#
# Each loop also re-asserts the priority invariants on EVERY iteration,
# so a shuffle can never quietly start outranking the tiering.

_RUNS = 10


def test_pick_session_presentation_order_varies(repos):
    """Same five cards every build (target_size == deck size), so the
    only thing that can differ is the order they're shown in."""
    deck_id, qids = _seed_trivia_deck(repos, n_questions=5)
    trivia = repos["trivia"]
    sequences = set()
    for _ in range(_RUNS):
        session = trivia.pick_session_for_deck(deck_id, target_size=5, fresh_target=5)
        ids = [c.question_id for c in session]
        # Invariant on every build: the whole deck, each card once, all fresh.
        assert sorted(ids) == sorted(qids)
        assert all(c.is_fresh for c in session)
        sequences.add(tuple(ids))
    assert len(sequences) > 1


def test_pick_session_shown_order_is_not_tier_order(repos):
    """The selection tiering must not leak into the SHOWN order.

    This deck pins the membership completely: one wrong card and one
    correct card fill the two review slots, one fresh card fills the
    fresh slot, so every build returns the same three cards. The only
    thing left that can move is the sequence: which makes this the
    test that fails if the final shuffle is dropped and the tiers are
    served in concatenation order (review, review, fresh) forever.

    K=3 leaves only 6 permutations, so this loop runs longer than its
    neighbours: a false red needs all 20 builds to agree, (1/6)^19 ≈
    2e-15.
    """
    deck_id, qids = _seed_trivia_deck(repos, n_questions=3)
    trivia = repos["trivia"]
    trivia.mark_answered(qids[0], correct=False)  # wrong → review slot
    trivia.mark_answered(qids[1], correct=True)  # correct → review slot
    # qids[2] is the only fresh card.
    sequences = set()
    wrong_positions = set()
    for _ in range(20):
        ids = [c.question_id for c in trivia.pick_session_for_deck(deck_id)]
        assert sorted(ids) == sorted(qids)  # membership is fixed by design
        sequences.add(tuple(ids))
        wrong_positions.add(ids.index(qids[0]))
    assert len(sequences) > 1
    # Sharper: the review tier does not permanently own the front of
    # the queue, and the fresh card is not permanently last.
    assert len(wrong_positions) > 1


def test_pick_session_selection_varies_within_tier(repos):
    """With more same-tier cards than slots, WHICH cards make the cut
    varies too: otherwise the same three fresh cards would be served
    forever and the rest of the deck would never surface."""
    deck_id, qids = _seed_trivia_deck(repos, n_questions=8)
    trivia = repos["trivia"]
    selections = set()
    for _ in range(_RUNS):
        session = trivia.pick_session_for_deck(deck_id, target_size=3)
        ids = [c.question_id for c in session]
        assert len(ids) == 3
        assert len(set(ids)) == 3
        assert set(ids) <= set(qids)
        assert all(c.is_fresh for c in session)
        selections.add(frozenset(ids))
    assert len(selections) > 1


def test_pick_session_fresh_slot_rotates_through_the_pool(repos):
    """The fresh slot must not always hand back the same card.

    Both review slots are filled by the only two answered cards and
    there is no backfill, so the fresh slot is the single degree of
    freedom: which of the six fresh cards fills it comes from the
    fresh pool's own tiebreak. Uniform over 6, so 15 identical picks
    is (1/6)^14 ≈ 1e-11.
    """
    deck_id, qids = _seed_trivia_deck(repos, n_questions=8)
    trivia = repos["trivia"]
    trivia.mark_answered(qids[0], correct=False)
    trivia.mark_answered(qids[1], correct=True)
    fresh_pool = set(qids[2:])
    seen_fresh = set()
    for _ in range(15):
        session = trivia.pick_session_for_deck(deck_id, target_size=3, fresh_target=1)
        fresh = [c for c in session if c.is_fresh]
        assert len(fresh) == 1  # the mix holds on every build
        assert fresh[0].question_id in fresh_pool
        seen_fresh.add(fresh[0].question_id)
    assert len(seen_fresh) > 1


def test_pick_session_review_slots_rotate_through_the_pool(repos):
    """Same for the review slots: with more answered cards than review
    slots, which ones come back varies instead of pinning the two
    lowest-numbered cards forever. C(4,2) = 6 possible pairs, so 15
    identical draws is (1/6)^14 ≈ 1e-11."""
    deck_id, qids = _seed_trivia_deck(repos, n_questions=6)
    trivia = repos["trivia"]
    for qid in qids[:4]:
        trivia.mark_answered(qid, correct=True)  # 4 correct, no wrong
    answered = set(qids[:4])
    pairs = set()
    for _ in range(15):
        session = trivia.pick_session_for_deck(deck_id, target_size=3, fresh_target=1)
        review = {c.question_id for c in session if not c.is_fresh}
        assert len(review) == 2
        assert review <= answered
        pairs.add(frozenset(review))
    assert len(pairs) > 1


def test_pick_session_priority_holds_on_every_shuffled_build(repos):
    """The shuffle must not cost us the tiering. Over many builds: the
    wrong-answered card is ALWAYS included, the fresh slot is ALWAYS
    filled, and the order still varies."""
    deck_id, qids = _seed_trivia_deck(repos, n_questions=6)
    trivia = repos["trivia"]
    trivia.mark_answered(qids[0], correct=False)  # wrong → must always come back
    trivia.mark_answered(qids[1], correct=True)  # correct → the other review slot
    fresh_pool = set(qids[2:])
    sequences = set()
    for _ in range(_RUNS):
        session = trivia.pick_session_for_deck(deck_id, target_size=3, fresh_target=1)
        ids = [c.question_id for c in session]
        assert qids[0] in ids, "wrong-answered card must survive the shuffle"
        assert qids[1] in ids
        fresh = [c for c in session if c.is_fresh]
        assert len(fresh) == 1
        assert fresh[0].question_id in fresh_pool
        sequences.add(tuple(ids))
    assert len(sequences) > 1


def test_pick_next_varies_within_tier(repos):
    """Single-card path: five equally-fresh cards, so the pick must
    move around instead of always handing back the first-inserted."""
    deck_id, qids = _seed_trivia_deck(repos, n_questions=5)
    trivia = repos["trivia"]
    picked = set()
    for _ in range(_RUNS):
        nxt = trivia.pick_next_for_deck(deck_id)
        assert nxt is not None
        assert nxt.question_id in qids
        assert nxt.is_fresh is True
        picked.add(nxt.question_id)
    assert len(picked) > 1


def test_pick_next_priority_holds_under_randomization(repos):
    """Randomizing the within-tier pick must not let a lower tier jump
    the queue: the lone wrong-answered card wins every single time."""
    deck_id, qids = _seed_trivia_deck(repos, n_questions=5)
    trivia = repos["trivia"]
    trivia.mark_answered(qids[0], correct=False)  # wrong
    trivia.mark_answered(qids[1], correct=True)  # correct
    for _ in range(_RUNS):
        nxt = trivia.pick_next_for_deck(deck_id)
        assert nxt.question_id == qids[0]
        assert nxt.is_fresh is False


def test_pick_next_correct_never_beats_fresh_under_randomization(repos):
    """Mirror of the tier above: once the wrong card is cleared, the
    correctly-answered card must stay behind the fresh ones on every
    randomized pick."""
    deck_id, qids = _seed_trivia_deck(repos, n_questions=5)
    trivia = repos["trivia"]
    trivia.mark_answered(qids[0], correct=True)
    fresh_pool = set(qids[1:])
    picked = set()
    for _ in range(_RUNS):
        nxt = trivia.pick_next_for_deck(deck_id)
        assert nxt.question_id in fresh_pool
        assert nxt.is_fresh is True
        picked.add(nxt.question_id)
    assert len(picked) > 1


# ---- count_unanswered + existing_prompts -------------------------------


def test_count_unanswered_drops_as_cards_are_seen(repos):
    deck_id, qids = _seed_trivia_deck(repos, n_questions=3)
    trivia = repos["trivia"]
    assert trivia.count_unanswered(deck_id) == 3
    trivia.mark_answered(qids[0], correct=True)
    assert trivia.count_unanswered(deck_id) == 2
    trivia.mark_answered(qids[1], correct=False)
    assert trivia.count_unanswered(deck_id) == 1
    trivia.mark_answered(qids[2], correct=True)
    assert trivia.count_unanswered(deck_id) == 0


def test_existing_prompts_returns_all(repos):
    deck_id, _qids = _seed_trivia_deck(repos, n_questions=3)
    prompts = repos["trivia"].existing_prompts(deck_id)
    assert len(prompts) == 3
    assert all("capital of country" in p for p in prompts)


# ---- TriviaSessionsRepo.abandon_all_for_deck --------------------------


def test_abandon_all_for_deck_active_session(repos):
    deck_id, qids = _seed_trivia_deck(repos, n_questions=3)
    user = repos["user"]
    sessions = TriviaSessionsRepo()
    sessions.start_or_resume(user, deck_id, queue=qids, done=[])
    n = sessions.abandon_all_for_deck(user, deck_id)
    assert n == 1
    assert sessions.get_active_for_deck(user, deck_id) is None


def test_abandon_all_for_deck_noop_when_none_active(repos):
    deck_id, _qids = _seed_trivia_deck(repos, n_questions=3)
    user = repos["user"]
    sessions = TriviaSessionsRepo()
    n = sessions.abandon_all_for_deck(user, deck_id)
    assert n == 0


def test_abandon_all_for_deck_scoped_to_user_and_deck(repos):
    deck_a, qids_a = _seed_trivia_deck(repos, name="alpha", n_questions=2)
    deck_b, qids_b = _seed_trivia_deck(repos, name="beta", n_questions=2)
    user = repos["user"]
    sessions = TriviaSessionsRepo()
    sessions.start_or_resume(user, deck_a, queue=qids_a, done=[])
    sessions.start_or_resume(user, deck_b, queue=qids_b, done=[])
    n = sessions.abandon_all_for_deck(user, deck_a)
    assert n == 1
    # Other deck's session is untouched.
    assert sessions.get_active_for_deck(user, deck_b) is not None


# ---- snooze --------------------------------------------------------


def test_snooze_active_for_deck_hides_from_list_active(repos):
    from datetime import datetime, timedelta, timezone

    deck_id, qids = _seed_trivia_deck(repos, n_questions=3)
    user = repos["user"]
    sessions = TriviaSessionsRepo()
    sessions.start_or_resume(user, deck_id, queue=qids, done=[])
    assert len(sessions.list_active(user)) == 1
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    n = sessions.snooze_active_for_deck(user, deck_id, future)
    assert n == 1
    assert sessions.list_active(user) == []


def test_snooze_active_for_deck_noop_when_no_session(repos):
    from datetime import datetime, timedelta, timezone

    deck_id, _qids = _seed_trivia_deck(repos, n_questions=3)
    user = repos["user"]
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    n = TriviaSessionsRepo().snooze_active_for_deck(user, deck_id, future)
    assert n == 0


def test_snooze_expired_resurfaces(repos):
    from datetime import datetime, timedelta, timezone

    deck_id, qids = _seed_trivia_deck(repos, n_questions=3)
    user = repos["user"]
    sessions = TriviaSessionsRepo()
    sessions.start_or_resume(user, deck_id, queue=qids, done=[])
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    sessions.snooze_active_for_deck(user, deck_id, past)
    assert len(sessions.list_active(user)) == 1


def test_list_snoozed_returns_snoozed_only(repos):
    from datetime import datetime, timedelta, timezone

    deck_id, qids = _seed_trivia_deck(repos, n_questions=3)
    user = repos["user"]
    sessions = TriviaSessionsRepo()
    sessions.start_or_resume(user, deck_id, queue=qids, done=[])
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    sessions.snooze_active_for_deck(user, deck_id, future)

    snoozed = sessions.list_snoozed(user)
    assert len(snoozed) == 1
    assert snoozed[0].deck_id == deck_id
    assert snoozed[0].snoozed_until == future
    assert sessions.list_active(user) == []


def test_snooze_none_wakes_trivia_session(repos):
    from datetime import datetime, timedelta, timezone

    deck_id, qids = _seed_trivia_deck(repos, n_questions=3)
    user = repos["user"]
    sessions = TriviaSessionsRepo()
    sessions.start_or_resume(user, deck_id, queue=qids, done=[])
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    sessions.snooze_active_for_deck(user, deck_id, future)
    assert len(sessions.list_snoozed(user)) == 1
    sessions.snooze_active_for_deck(user, deck_id, None)
    assert sessions.list_snoozed(user) == []
    assert len(sessions.list_active(user)) == 1


def test_pick_session_does_not_re_serve_a_just_answered_card(repos):
    """Randomizing ties must not flatten recency: a card answered
    seconds ago should not come straight back while cards answered
    long ago are waiting.

    The tie-break is bucketed by hour, so "answered in this sitting"
    and "answered last month" are genuinely different keys and only
    same-hour answers shuffle against each other. Without the bucket
    (a bare RANDOM() over the whole answered pool) a 3-card session
    would routinely re-serve what the user just finished.
    """
    deck_id, qids = _seed_trivia_deck(repos, n_questions=6)
    trivia = repos["trivia"]
    long_ago, just_now = set(qids[:3]), set(qids[3:])
    from prep.infrastructure.db import cursor

    with cursor() as c:
        for qid in long_ago:
            c.execute(
                """UPDATE trivia_queue
                      SET last_answered_at = '2026-01-01T00:00:00',
                          last_answered_correctly = 1
                    WHERE question_id = ?""",
                (qid,),
            )
    for qid in just_now:
        trivia.mark_answered(qid, correct=True)

    orders = set()
    for _ in range(20):
        picked = trivia.pick_session_for_deck(deck_id, target_size=2, fresh_target=0)
        ids = [p.question_id for p in picked]
        assert len(ids) == 2, "the review pool should fill both slots"
        assert not (set(ids) & just_now), (
            "a card answered in this sitting was re-served ahead of cards answered long ago"
        )
        orders.add(tuple(ids))

    # The three long-ago cards share one bucket, so which two are
    # picked (and in what order) must still vary. 20 runs over 6
    # possible outcomes: a false red needs every run to agree,
    # (1/6)^19, which is not a thing that happens.
    assert len(orders) > 1, "same-bucket cards are not being shuffled"
