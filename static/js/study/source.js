// source.js: the CardSource port and its two implementations,
// LocalSource (IndexedDB) and ServerSource (the JSON study API). The
// study components never touch storage or the network; they talk to a
// source through this contract, so the same views run on both
// surfaces.
//
// CardSource contract (all methods async):
//
//   next() -> {card, draft}                 the next due card
//           | {caughtUp: {nextDueMinutes}}  nothing due; minutes until
//                                           the next future due, or
//                                           null when none is scheduled
//           | {verdict, ...}                a verdict already waiting
//           | {pending: Pending}            a grade already in flight
//           | {ended: {reason}}             the session is over
//
//     The last three reach a host only from ServerSource: server
//     sessions carry state across devices, so a resumed session can
//     land on any screen of the loop. LocalSource returns only the
//     first two.
//
//     An outcome key is NOT a tag: a settled verdict and a selfGrade
//     both carry the revealed `card` alongside, so a host must test
//     the keys in order (pending, verdict, selfGrade, ended, caughtUp,
//     card) or a verdict reads as a fresh card to answer.
//
//   submit(card, submission) -> outcome
//     submission: {answer}           grade this answer
//                 {idk: true}        "I don't know"
//                 {verdict, answer}  a self-grade verdict
//     outcome:    {verdict, nextDueMinutes, idk}  verdict recorded
//                 {selfGrade: true}  no deterministic grader; show the
//                                    reveal, then submit a {verdict}
//                 {pending: Pending} an async grader is running
//
//   author(input) -> the stored card row
//     input: {prompt, answer, deck_id}
//
// Pending: {settled, cancel, poll, workflowId}
//
//   THE SOURCE OWNS THE POLLING LOOP. A host renders the pending view
//   and awaits `settled`, which resolves to a settled outcome
//   ({verdict, nextDueMinutes, idk, card, answer}) or rejects with a
//   StudySourceError. Polling is I/O (intervals, backoff, retry across
//   a network blip, abort), which belongs behind the port with every
//   other request; a view that owned it would have to reimplement all
//   of it per host. `cancel()` stops the loop when the host leaves the
//   screen. LocalSource grades in-process and never returns {pending},
//   so a host written against the promise works on both sources.
//
// Failures from ServerSource are StudySourceError, whose `code` a host
// can branch on: network, unauthorized, not_found, stale_version
// (carries currentVersion), grading_failed, grading_timeout, cancelled,
// server, bad_response.

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
  const review = {
    client_id: uuid(),
    verdict,
    user_answer: userAnswer,
    graded_by: gradedBy,
    reviewed_at: reviewedAt,
  };
  if (isLocal) review.card_client_id = card.client_id;
  else review.question_id = card.question_id;
  const updated = await withLock(async () => {
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
  state.outbox.push(review);
  return t;
}

// ---- the local source --------------------------------------------------

// CardSource over the offline stores. Shares the host's mutable state
// object ({cards, localCards, outbox}): the host owns loading it
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

// ---- the server source -------------------------------------------------

// Every failure a host may want to act on differently, carried as a
// `code` rather than a message string. `detail` fields land on the
// instance (stale_version carries currentVersion).
export class StudySourceError extends Error {
  constructor(code, message, detail) {
    super(message || code);
    this.name = "StudySourceError";
    this.code = code;
    if (detail) Object.assign(this, detail);
  }
}

// The deploy's root path, derived from this module's own URL: the
// module is served under <root>/static/js/[v<build>/]study/source.js.
const ROOT_PATH = new URL(import.meta.url).pathname.replace(/\/static\/js\/.*$/, "");

// Grading polling. The interval widens as the wait goes on so a slow
// grade costs few requests, and the deadline stops a wedged workflow
// from polling until the tab closes.
const POLL_MIN_MS = 700;
const POLL_MAX_MS = 4000;
const POLL_GROWTH = 1.4;
const POLL_DEADLINE_MS = 300000;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// CardSource over the JSON study API (prep/study/api.py). Holds no DOM
// and no view state: just the deck/session it is pointed at, the
// session's optimistic-concurrency version, and fetch.
//
// Session methods (begin/advance/saveDraft/abandon/snooze) have no
// LocalSource counterpart. A host that drives sessions calls them
// directly; the three contract methods work with or without one.
export class ServerSource {
  constructor({deck = null, sessionId = null, basePath = ROOT_PATH, fetchImpl = null} = {}) {
    this.deck = deck;
    this.basePath = basePath;
    this._fetch = fetchImpl || ((...args) => fetch(...args));
    this._session = sessionId ? {id: sessionId, version: 0, state: null, status: null} : null;
  }

  // The last session payload the server sent, or null on the
  // deck-scoped (sessionless) path.
  get session() {
    return this._session;
  }

  // ---- the CardSource contract ----

  async next() {
    if (!this._session) {
      if (!this.deck) throw new StudySourceError("no_target", "no deck or session to study");
      return this._view(await this._request("GET", this._url(`/decks/${enc(this.deck)}/next`)));
    }
    // A session parked on a verdict has to be advanced past it: the
    // read endpoint would hand back that same verdict forever.
    if (this._session.state === "showing-result") return this.advance();
    return this._view(
      await this._request("GET", this._url(`/sessions/${enc(this._session.id)}/next`))
    );
  }

  async submit(card, submission) {
    const body = {
      question_id: card.question_id,
      answer: submission.answer || "",
      idk: Boolean(submission.idk),
    };
    if (submission.verdict) body.verdict = submission.verdict;
    const path = this._session
      ? `/sessions/${enc(this._session.id)}/submit`
      : `/decks/${enc(this.deck)}/submit`;
    if (this._session) body.version = this._session.version;
    return this._view(await this._request("POST", this._url(path), body));
  }

  async author(input) {
    const body = await this._request("POST", this._url("/cards"), {
      prompt: input.prompt,
      answer: input.answer,
      deck_id: input.deck_id ?? null,
    });
    return body.card;
  }

  // ---- session control ----

  // Resume the open session on the deck, or start a new one, and
  // return the view to render. `fresh` abandons the open one first.
  async begin({fresh = false} = {}) {
    if (!this.deck) throw new StudySourceError("no_target", "no deck to begin a session on");
    const body = await this._request("POST", this._url(`/decks/${enc(this.deck)}/session`), {
      fresh: Boolean(fresh),
    });
    return this._view(body);
  }

  async advance() {
    const s = this._requireSession();
    return this._view(
      await this._request("POST", this._url(`/sessions/${enc(s.id)}/advance`), {
        version: s.version,
      })
    );
  }

  // Autosave the in-progress answer. Returns the new version, which is
  // also cached, so the next submit carries it.
  async saveDraft(draft) {
    const s = this._requireSession();
    const body = await this._request("POST", this._url(`/sessions/${enc(s.id)}/draft`), {
      version: s.version,
      draft,
    });
    if (typeof body.version === "number") this._session = {...this._session, version: body.version};
    return body.version;
  }

  async abandon() {
    const s = this._requireSession();
    return this._view(await this._request("POST", this._url(`/sessions/${enc(s.id)}/abandon`)));
  }

  async snooze({preset = null, custom = null, unit = null} = {}) {
    const s = this._requireSession();
    return this._view(
      await this._request("POST", this._url(`/sessions/${enc(s.id)}/snooze`), {
        preset,
        custom,
        unit,
      })
    );
  }

  // ---- internals ----

  _requireSession() {
    if (!this._session) throw new StudySourceError("no_target", "no session on this source");
    return this._session;
  }

  _url(path) {
    return `${this.basePath}/api/study${path}`;
  }

  // Cache the session the server just described, so version, state and
  // deck name stay in step with it without the host relaying them.
  _absorb(body) {
    if (body && body.session) this._session = body.session;
    return body;
  }

  // Absorb, then hand back a view a host can branch on. A {pending}
  // payload becomes a Pending with its polling loop already running.
  _view(body) {
    this._absorb(body);
    if (body && body.pending) return {pending: this._pending(body.pending)};
    return body;
  }

  _pending(payload) {
    const control = {cancelled: false};
    const pending = {
      poll: payload.poll,
      workflowId: payload.workflow_id || "",
      // What the grader is reporting while it still runs, when it
      // reports anything. The view shows it; nothing branches on it.
      error: payload.error || "",
      cancel: () => {
        control.cancelled = true;
      },
    };
    pending.settled = this._pollUntilSettled(payload.poll, control);
    // The host may never await `settled` (it left the screen before
    // the grade landed), and a lone rejection would surface as an
    // unhandled rejection. This branch marks the promise handled; the
    // host's own await still sees the rejection.
    pending.settled.catch(() => {});
    return pending;
  }

  async _pollUntilSettled(poll, control) {
    const deadline = Date.now() + POLL_DEADLINE_MS;
    let wait = POLL_MIN_MS;
    for (;;) {
      await sleep(wait);
      if (control.cancelled) throw new StudySourceError("cancelled", "grading poll cancelled");
      if (Date.now() > deadline) {
        throw new StudySourceError("grading_timeout", "the grader did not answer in time");
      }
      let body;
      try {
        body = await this._request("GET", poll);
      } catch (e) {
        // The workflow is durable, so a dropped poll is not a lost
        // grade: keep asking until the deadline. An auth failure is
        // not transient and ends the loop.
        if (e instanceof StudySourceError && e.code === "network") {
          wait = Math.min(Math.round(wait * POLL_GROWTH), POLL_MAX_MS);
          continue;
        }
        throw e;
      }
      if (body.pending) {
        wait = Math.min(Math.round(wait * POLL_GROWTH), POLL_MAX_MS);
        continue;
      }
      if (body.failed) {
        throw new StudySourceError(
          body.failed.code || "grading_failed",
          body.failed.message || "the grader returned nothing"
        );
      }
      return this._absorb(body);
    }
  }

  async _request(method, url, body) {
    const headers = {Accept: "application/json"};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    let res;
    try {
      res = await this._fetch(url, {
        method,
        headers,
        // Identity rides cookies or a proxy-injected header, both of
        // which need the request treated as same-origin credentialed.
        credentials: "same-origin",
        cache: "no-store",
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (e) {
      throw new StudySourceError("network", "could not reach the server", {cause: e});
    }
    return this._parse(res, await res.text().catch(() => ""));
  }

  _parse(res, text) {
    let payload = null;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch (e) {
        payload = null;
      }
    }
    // Accept: application/json makes the app answer 401 with JSON
    // rather than bouncing to the identity provider, so an expired
    // session is a status code here. A redirect that still landed on
    // a non-JSON body is the same condition on a deploy that bounces
    // earlier, in middleware.
    if (res.status === 401 || res.status === 403) {
      throw new StudySourceError("unauthorized", "you are no longer signed in");
    }
    if (res.redirected && payload === null) {
      throw new StudySourceError("unauthorized", "you are no longer signed in");
    }
    const err = (payload && payload.error) || {};
    if (res.status === 409 && err.code === "stale_version") {
      const current = typeof err.current_version === "number" ? err.current_version : null;
      // Adopt the server's version so a retry is not stale for the
      // same reason twice; the host still gets the error and decides
      // whether to re-read or resubmit.
      if (this._session && current !== null) {
        this._session = {...this._session, version: current};
      }
      throw new StudySourceError("stale_version", err.message || "the session moved on", {
        currentVersion: current,
      });
    }
    if (res.status === 404) {
      throw new StudySourceError("not_found", err.message || "not found");
    }
    if (!res.ok) {
      throw new StudySourceError(
        err.code || "server",
        err.message || `request failed (${res.status})`,
        {status: res.status}
      );
    }
    if (payload === null) {
      throw new StudySourceError("bad_response", "the server did not answer with JSON");
    }
    return payload;
  }
}

function enc(value) {
  return encodeURIComponent(String(value));
}
