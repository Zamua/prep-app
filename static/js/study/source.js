// source.js: the CardSource port and its local (IndexedDB)
// implementation. The study components never touch storage; they
// talk to a source through this contract, so the same views can run
// against the offline stores today and an online source later.
//
// CardSource contract (all methods async):
//
//   next() -> {card}                        the next due card
//           | {caughtUp: {nextDueMinutes}}  nothing due; minutes until
//                                           the next future due, or
//                                           null when none is scheduled
//
//   submit(card, submission) -> outcome
//     submission: {answer}           grade this answer
//                 {idk: true}        "I don't know"
//                 {verdict, answer}  a self-grade verdict
//     outcome:    {verdict, nextDueMinutes, idk}  verdict recorded
//                 {selfGrade: true}  no deterministic grader; show the
//                                    reveal, then submit a {verdict}
//                 {pending: ...}     reserved for async graders;
//                                    LocalSource never returns it
//
//   author(input) -> the stored card row
//     input: {prompt, answer, deck_id}

import {get, put, uuid, withLock} from "../offline/store.js";
import * as grader from "../offline/grader.js";
import * as scheduler from "../offline/scheduler.js";

// ---- due-time math ---------------------------------------------------

// A card's effective due time offline: the local ladder overlay when
// set, else the snapshot's server-computed next_due.
export function effectiveDue(card) {
  return card.local_next_due || card.next_due || null;
}

// Due now = effective due parses and is in the past (via the ladder's
// due()). A null due (a card the server considers due immediately)
// counts as due; an unparseable timestamp does not (junk must not
// flood the queue).
export function isDueNow(card, now) {
  // scheduler.due owns the whole contract, including the fail-open
  // rule: a missing or unparseable next_due counts as DUE. Filtering
  // junk out here would silently vanish the card from the queue and
  // deck counts forever; surfacing it costs one early review.
  return scheduler.due(now, effectiveDue(card));
}

function dueTime(card) {
  const dueAt = effectiveDue(card);
  if (dueAt === null) return 0;
  const t = Date.parse(dueAt);
  return Number.isFinite(t) ? t : 0;
}

// ---- study ordering --------------------------------------------------
//
// Priority stays the selection rule: a genuinely more-overdue card
// still comes first. What was stale is the TIE: a deck created in one
// batch comes due all at once, so sorting on due time alone left the
// order to array position and every sitting replayed the same
// sequence.
//
// "Tie" has to mean same-MINUTE, not same-millisecond. Each card's
// next_due is stamped from its own clock read, so a batch's
// timestamps land microseconds apart and a millisecond comparison is
// a total order in creation order: the tiebreak would never fire.
// DUE_BUCKET_MS mirrors the server's _DUE_BUCKET (prep/study/repo.py)
// so the offline queue and the online queue agree on what counts as
// equally due: the wall-clock hour, wide enough that a generation
// batch spanning seconds cannot split across buckets and replay in
// creation order.
//
// The queue is recomputed on every render, so a random comparator
// would reshuffle cards mid-sitting and make them jump between
// screens. Instead each card gets ONE random key the first time it is
// seen in this page load; the key lives until reload, which is the
// span of a single sitting.

const DUE_BUCKET_MS = 3600000;

const shuffleKeys = new Map();

// Snapshot cards are identified by question_id, locally authored ones
// by client_id (a local card has no question_id). Namespaced so the
// two id spaces can never collide.
function cardIdentity(card) {
  return card.client_id ? "local:" + card.client_id : "q:" + card.question_id;
}

function shuffleKey(card) {
  const id = cardIdentity(card);
  let key = shuffleKeys.get(id);
  if (key === undefined) {
    key = Math.random();
    shuffleKeys.set(id, key);
  }
  return key;
}

// The minute a card came due. Cards sharing one bucket are treated as
// equally due and get shuffled against each other.
function dueBucket(card) {
  return Math.floor(dueTime(card) / DUE_BUCKET_MS);
}

// Oldest effective due first; random-but-stable within the minute.
export function compareStudyOrder(a, b) {
  return dueBucket(a) - dueBucket(b) || shuffleKey(a) - shuffleKey(b);
}

// Minutes until the next FUTURE due among `cards`, or null when
// nothing is scheduled.
export function nextDueInMinutes(cards) {
  const now = Date.now();
  let best = null;
  for (const card of cards) {
    const t = Date.parse(effectiveDue(card) || "");
    if (!Number.isFinite(t) || t <= now) continue;
    if (best === null || t < best) best = t;
  }
  if (best === null) return null;
  return Math.max(1, Math.ceil((best - now) / 60000));
}

// ---- the study ledger ------------------------------------------------

// Every verdict writes two things (docs/OFFLINE.md section 2): the
// queued review for sync, and the local ladder overlay so the card
// re-surfaces offline. The transition is computed first (pure), then
// the outbox row, then the overlay; if the overlay write loses a
// race with a crash the card just comes back early, while the review
// itself is already safely queued.
async function recordVerdict(state, card, verdict, userAnswer, gradedBy) {
  // reviewed_at MUST be new Date().toISOString(): flushOutbox orders
  // rows by LEXICOGRAPHIC reviewed_at comparison, which is only
  // chronological when every timestamp is uniform-offset UTC ISO-8601.
  const reviewedAt = new Date().toISOString();
  const seedStep = card.local_step ?? card.step ?? 0;
  const t = scheduler.transition(seedStep, verdict);
  // A locally authored card is identified by its client UUID; the
  // server resolves card_client_id through the created card's
  // idempotency mapping at sync time (docs/OFFLINE.md section 4).
  const isLocal = Boolean(card.client_id);
  // Locked against sync.js's snapshot-refresh overlay merge: a
  // refresh in flight between our outbox write and overlay write
  // would wipe the overlay this tap creates (its pending-ids
  // snapshot predates us). The lock makes tap and merge take turns.
  const updated = await withLock(async () => {
    const review = {
      client_id: uuid(),
      verdict,
      user_answer: userAnswer,
      graded_by: gradedBy,
      reviewed_at: reviewedAt,
    };
    if (isLocal) review.card_client_id = card.client_id;
    else review.question_id = card.question_id;
    await put("outbox_reviews", review);
    const row = {
      ...card,
      local_step: t.step,
      // nextDueIso emits the same uniform-offset UTC shape as
      // toISOString, keeping every timestamp we write on the
      // lexicographic-ordering contract by construction.
      local_next_due: scheduler.nextDueIso(Date.now(), t.next_due_minutes),
    };
    // The local ladder overlay lives ON the local_cards row for an
    // authored card (the snapshot knows nothing about it yet). But a
    // stale closure can outlive the row: if a background sync
    // created the card and deleted the row while this view was open,
    // re-putting it would resurrect a zombie copy next to the
    // snapshot card. The review above still syncs fine by
    // card_client_id (the server resolves it via the idempotency
    // mapping), so on a missing row we skip the overlay write.
    if (isLocal) {
      const live = await get("local_cards", card.client_id);
      if (live) await put("local_cards", row);
    } else {
      await put("cards", row);
    }
    return row;
  });
  if (isLocal) {
    const i = state.localCards.findIndex((c) => c.client_id === card.client_id);
    if (i !== -1) state.localCards[i] = updated;
  } else {
    const i = state.cards.findIndex((c) => c.question_id === card.question_id);
    if (i !== -1) state.cards[i] = updated;
  }
  state.outboxCount += 1;
  return t;
}

// ---- the local source --------------------------------------------------

// CardSource over the offline stores. Shares the host's mutable state
// object ({cards, localCards, outboxCount}): the host owns loading it
// from IndexedDB and rendering from it; submits and authors mutate
// both IndexedDB and the in-memory mirror, exactly like the pre-port
// inline flow did.
export class LocalSource {
  constructor(state) {
    this.state = state;
  }

  allCards() {
    return this.state.cards.concat(this.state.localCards);
  }

  // The queue is recomputed on every advance (docs/OFFLINE.md section
  // 2): oldest effective due first, so a card answered wrong (+10m)
  // naturally returns later in a long sitting. Cards sharing a due
  // time fall back to their per-sitting shuffle key, so the batch
  // that was generated together varies run to run without jumping
  // around mid-sitting.
  async next() {
    const now = Date.now();
    const dueCards = this.allCards().filter((card) => isDueNow(card, now));
    if (!dueCards.length) {
      return {caughtUp: {nextDueMinutes: nextDueInMinutes(this.allCards())}};
    }
    dueCards.sort(compareStudyOrder);
    return {card: dueCards[0]};
  }

  async submit(card, submission) {
    if (submission.verdict) {
      const t = await recordVerdict(
        this.state, card, submission.verdict, submission.answer, "self"
      );
      return {verdict: submission.verdict, nextDueMinutes: t.next_due_minutes, idk: false};
    }
    if (submission.idk) {
      // "I don't know": wrong verdict with an empty answer, same as
      // the online idk path. Deterministic, so graded_by stays "auto".
      const t = await recordVerdict(this.state, card, "wrong", "", "auto");
      return {verdict: "wrong", nextDueMinutes: t.next_due_minutes, idk: true};
    }
    let graded = null;
    try {
      graded = grader.grade(card, submission.answer);
    } catch (e) {
      graded = null; // an ungradeable card falls through to self-verdict
    }
    if (graded && graded.verdict) {
      const t = await recordVerdict(this.state, card, graded.verdict, submission.answer, "auto");
      return {verdict: graded.verdict, nextDueMinutes: t.next_due_minutes, idk: false};
    }
    return {selfGrade: true};
  }

  // Writes a local_cards row that is due immediately (local_next_due
  // null), mirroring the online "shows up as due immediately"
  // behavior for manual authoring; sync.js sends it as a new_cards
  // item ahead of any review that references it.
  async author(input) {
    const row = {
      client_id: uuid(),
      deck_id: input.deck_id ?? null,
      prompt: input.prompt,
      answer: input.answer,
      created_at: new Date().toISOString(),
      local_step: 0,
      local_next_due: null,
    };
    // Locked for the same reason as recordVerdict: local_cards rows
    // must not be written while flushOutbox is mid-drain deciding
    // which rows it already sent.
    await withLock(() => put("local_cards", row));
    this.state.localCards.push(row);
    return row;
  }
}
