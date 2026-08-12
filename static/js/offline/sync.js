// sync.js: snapshot refresh + outbox flush. The one shared seam
// between the online app and the offline shell: app.js imports it on
// every online page (via init below); the offline app only reads what
// it wrote.
//
// The flush reads local_cards (written by the M4 authoring form) and
// outbox_reviews (written by the M3 study surface), POSTs batches to
// /api/offline/sync, prunes acked items, and records rejects. Cards
// go FIRST: cards-before-reviews is guaranteed by the server only
// within one request, so every new_cards chunk must be flushed and
// acked before the first reviews chunk is sent, or a review whose
// card_client_id sits in a later chunk would reject as unknown. The
// owner-mismatch guard ships with the flush, not after it: syncing
// under a mismatched sign-in would replay one account's outbox into
// another. On mismatch, sync disables entirely for the session -- no
// flush, no snapshot write -- and the M5 confirm-then-wipe dialog
// (maybeConfirmOwnerConflict below) offers the signed-in user an
// EXPLICIT choice: wipe this device's offline data and reseed as the
// new account, or keep the other account's data with sync off. Never
// silent, never automatic.
//
// Owner-ABSENT guest data (an anonymous instant-start deck) is the
// other identity edge: it must not flush into whichever account signs
// in, and the snapshot must not stamp meta.owner over it. Both writes
// stay behind guestAdoptionPending until the adoption dialog decides.
// Absent is adoptable, mismatch refuses; they are different answers.

import {clear, get, getAll, bulkReplace, metaGet, metaPut, put, remove, wipeAll, withLock} from "./store.js";

const REFRESH_INTERVAL_MS = 60 * 60 * 1000;

// Server caps are 100 cards and 500 reviews per request; the client
// chunks under them, reviews in reviewed_at order (docs/OFFLINE.md
// section 4).
const CARDS_PER_CHUNK = 100;
const REVIEWS_PER_CHUNK = 500;

// Flipped on owner mismatch; every sync entry point then refuses to
// touch the network or the local stores until the next page load (or
// until the user chooses wipe-and-reseed in the conflict dialog).
let syncDisabled = false;

// Set by ownerAllows when the guard trips: {owner, serverUser}. The
// raw material for maybeConfirmOwnerConflict; null while no mismatch
// has been observed this session.
let ownerConflict = null;

// Opened by the adoption dialog's Accept so its own flush and forced
// refresh can pass guestAdoptionPending. Memory-only on purpose: an
// adoption interrupted mid-flush loses the latch and re-prompts on
// the next authenticated load, where the idempotent sync machinery
// makes the retried flush a pure replay.
let adoptionApproved = false;

// The deploy's root path, derived from this module's own URL (it is
// served under <root>/static/js/..., versioned prefix included).
const ROOT_PATH = new URL(import.meta.url).pathname.replace(/\/static\/js\/.*$/, "");

// The build token, when this module was loaded through the versioned
// URL space (the importmap's "@/" prefix or the shell's importmap).
// Null when unversioned (dev direct loads); the owner record simply
// records what we know.
function buildToken() {
  const m = new URL(import.meta.url).pathname.match(
    /\/static\/js\/v([0-9a-f]{7,40}|[0-9]+)\//
  );
  return m ? m[1] : null;
}

// The load-bearing identity guard: the server-resolved user id must
// match the local snapshot owner before ANY flush or snapshot write.
// A device with no owner yet (fresh install) passes; a mismatch
// disables sync for the whole session so the mismatched sign-in can
// neither absorb the other account's outbox nor overwrite its data.
async function ownerAllows(serverUser) {
  const owner = await metaGet("owner");
  const serverUserId = serverUser && serverUser.id;
  if (owner && owner.user_id && serverUserId && owner.user_id !== serverUserId) {
    syncDisabled = true;
    ownerConflict = {owner, serverUser};
    console.warn(
      "offline sync disabled: signed-in user does not match this device's snapshot owner"
    );
    return false;
  }
  return true;
}

// The owner-absent counterpart of ownerAllows: guest data on a device
// with no snapshot owner blocks BOTH the flush (it would silently
// absorb the guest deck into whichever account signed in) and the
// snapshot write (stamping meta.owner would destroy the owner-absent
// signal and turn the NEXT flush into a silent adoption). Guest data
// present = meta.guest exists, or local_cards is non-empty while the
// owner is absent.
export async function guestAdoptionPending() {
  if (adoptionApproved) return false;
  if (await metaGet("owner")) return false;
  if (await metaGet("guest")) return true;
  return (await getAll("local_cards")).length > 0;
}

async function fetchSnapshotPayload() {
  const response = await fetch(ROOT_PATH + "/api/offline/snapshot", {
    credentials: "same-origin",
    headers: {accept: "application/json"},
  });
  if (response.status !== 200) return {ok: false, status: response.status};
  const snapshot = await response.json();
  if (!snapshot || !snapshot.user || !snapshot.user.id) {
    return {ok: false, status: 200, malformed: true};
  }
  return {ok: true, snapshot};
}

// Fetch GET /api/offline/snapshot and fully replace the local decks +
// cards stores, then stamp meta.owner. Returns a small result object;
// never intended to run in a user-blocking path.
//
// Throttled: skipped when the last successful refresh is younger than
// an hour, unless force is set (a post-flush refresh will force).
export async function refreshSnapshot({force = false} = {}) {
  if (syncDisabled) return {ok: false, disabled: true};
  if (await guestAdoptionPending()) return {ok: false, adoption_pending: true};
  if (!force) {
    const sync = await metaGet("sync");
    const ownerMeta = await metaGet("owner").catch(() => null);
    if (sync && sync.last_refresh_at && !ownerMeta) {
      const age = Date.now() - Date.parse(sync.last_refresh_at);
      if (Number.isFinite(age) && age >= 0 && age < REFRESH_INTERVAL_MS) {
        return {ok: true, skipped: true};
      }
    }
  }

  // Anything but a clean 200 (signed-out 401, transient 5xx) leaves
  // the local snapshot untouched; it stays disposable and re-fetchable.
  const fetched = await fetchSnapshotPayload();
  if (!fetched.ok) return fetched;
  const snapshot = fetched.snapshot;
  if (!(await ownerAllows(snapshot.user))) return {ok: false, disabled: true};

  // Full replace sidesteps tombstone bookkeeping for deletions. Local
  // ladder overlays are seeded null EXCEPT for cards that still have
  // reviews queued in the outbox: those keep their overlay (and only
  // those). Without this, the forced refresh after a PARTIAL flush
  // would snap unsynced cards back to the server's stale next_due,
  // resurfacing cards the user already studied offline. Cards whose
  // reviews all flushed converge to the server's FSRS truth here.
  // The whole read-merge-replace runs under the store lock so a
  // verdict tap on the interactive offline page cannot land between
  // our pending-ids snapshot and the cards replace (it would be
  // wiped; the lock makes the tap wait its turn and survive).
  await withLock(async () => {
    const queuedReviews = await getAll("outbox_reviews");
    // A queued review pins its card's overlay through the replace. It
    // names the card either by question_id (snapshot card) or by
    // resolved_question_id (an offline-authored card whose creation
    // acked in a partial flush; flushOutbox stamps the mapping so the
    // still-queued review keeps protecting the overlay it carried to
    // the cards store).
    const pendingIds = new Set(
      queuedReviews
        .map((r) => r.question_id ?? r.resolved_question_id)
        .filter((id) => id !== undefined && id !== null)
    );
    let overlays = new Map();
    if (pendingIds.size) {
      const existing = await getAll("cards");
      overlays = new Map(
        existing
          .filter((card) => pendingIds.has(card.question_id))
          .map((card) => [card.question_id, card])
      );
    }
    await bulkReplace("decks", snapshot.decks || []);
    await bulkReplace(
      "cards",
      (snapshot.cards || []).map((card) => {
        const prev = overlays.get(card.question_id);
        return {
          ...card,
          local_step: prev ? (prev.local_step ?? null) : null,
          local_next_due: prev ? (prev.local_next_due ?? null) : null,
        };
      })
    );
  });
  await metaPut("owner", {
    user_id: snapshot.user.id,
    display_name: snapshot.user.display_name || "",
    snapshot_at: snapshot.generated_at || new Date().toISOString(),
    build: buildToken(),
  });
  await metaPut("sync", {last_refresh_at: new Date().toISOString()});
  // An Accept interrupted between the owner stamp and its meta.guest
  // deletion leaves debris no other path would ever clear: every
  // guest surface is owner-absent-gated, so with the owner stamped
  // the record is dead weight. Sweep it (delete is a no-op when
  // absent).
  await remove("meta", "guest");
  // Ask the platform to shield this origin's storage from eviction
  // (docs/OFFLINE.md section 3, "Storage persistence and eviction
  // margin"): fire-and-forget after a successful snapshot write, so
  // the request always follows real data worth keeping.
  requestPersistence();
  return {ok: true};
}

// navigator.storage.persist(), guarded and swallowed: persistence is
// a margin-widener, never a correctness dependency. Skips the call
// when the grant already exists so repeated refreshes cannot nag on
// engines that surface a permission prompt.
async function requestPersistence() {
  try {
    if (!navigator.storage || !navigator.storage.persist) return;
    const already = await navigator.storage.persisted().catch(() => false);
    if (already) return;
    // Ask ONCE per device (spec: "on the first successful snapshot
    // write"). Browsers with prompt-based UX (Firefox) would re-nag
    // on every hourly refresh otherwise. The stamp dies with a wipe,
    // which is the right moment to ask again anyway.
    const stamped = await metaGet("persist_requested").catch(() => null);
    if (stamped) return;
    await metaPut("persist_requested", {at: new Date().toISOString()});
    navigator.storage.persist().catch(() => {});
  } catch (e) {
    // cosmetic; never let a storage API quirk break sync
  }
}

// Project a local_cards record onto the new_cards wire shape: the
// local ladder overlay fields (local_step, local_next_due) stay home;
// the server stamps its own creation time and starts the card due
// immediately regardless.
function toWireCard(item) {
  const wire = {
    client_id: item.client_id,
    deck_id: item.deck_id ?? null,
    prompt: item.prompt,
    answer: item.answer,
    created_at: item.created_at,
  };
  // Guest-generated rows carry a deck label and a regex; the server
  // re-validates both. Rows without them (plain authored cards) keep
  // today's wire shape exactly.
  if (item.deck_name != null) wire.deck_name = item.deck_name;
  if (item.answer_regex != null) wire.answer_regex = item.answer_regex;
  return wire;
}

// Project an outbox record onto the wire shape: exactly one of
// question_id / card_client_id identifies the target; local
// bookkeeping fields (sync_status, ...) stay home.
function toWireReview(item) {
  const wire = {
    client_id: item.client_id,
    verdict: item.verdict,
    user_answer: item.user_answer || "",
    graded_by: item.graded_by,
    reviewed_at: item.reviewed_at,
  };
  if (item.card_client_id) wire.card_client_id = item.card_client_id;
  else wire.question_id = item.question_id;
  return wire;
}

// The one exception to "rejected moves to the rejects store": a
// review rejected for an unknown card_client_id whose card still sits
// queued in local_cards means the card has not been created yet (an
// interrupted flush). It stays in the outbox; the next flush sends
// cards first and resolves it.
async function awaitingLocalCard(item, result) {
  if (!item.card_client_id) return false;
  if (!/unknown card_client_id/.test(result.error || "")) return false;
  const localCard = await get("local_cards", item.card_client_id);
  return Boolean(localCard);
}

// Pre-validate a store's rows: a row whose client_id is not a string
// cannot be matched against the server's echoed results (IDB keys may
// be numbers), so it would be re-sent and re-rejected on every flush
// forever. Park corrupt rows in rejects before POSTing anything;
// returns the surviving rows.
async function parkCorruptRows(storeName, rows, kind) {
  const corrupt = rows.filter((r) => typeof r.client_id !== "string" || !r.client_id);
  for (const row of corrupt) {
    await put("rejects", {
      client_id: String(row.client_id ?? "corrupt-" + Date.now()),
      kind,
      error: "corrupt " + storeName + " row (non-string client_id)",
      item: row,
    });
    await remove(storeName, row.client_id);
  }
  if (!corrupt.length) return rows;
  return rows.filter((r) => typeof r.client_id === "string" && r.client_id);
}

// One POST to the sync endpoint. Returns the parsed body, or null on
// a transient failure (network flap, non-200): the caller leaves
// everything still queued -- the server's idempotency table makes the
// eventual retry a pure replay.
async function postSyncChunk(deviceId, newCards, reviews) {
  let response;
  try {
    response = await fetch(ROOT_PATH + "/api/offline/sync", {
      method: "POST",
      credentials: "same-origin",
      headers: {"content-type": "application/json", accept: "application/json"},
      body: JSON.stringify({device_id: deviceId, new_cards: newCards, reviews}),
    });
  } catch (e) {
    return null;
  }
  if (response.status !== 200) return null;
  return response.json();
}

// Flush the outbox: POST queued local_cards as new_cards chunks
// FIRST (every card chunk acked before the first review is sent,
// docs/OFFLINE.md section 4), then queued reviews in reviewed_at
// order, all in chunks under the server caps. Acked items (created /
// applied / logged_no_reschedule) leave their store -- a created
// card's row is deleted and the forced post-flush snapshot refresh
// delivers it back as a real snapshot card. Permanent rejects move
// to the rejects store for the needs-attention list; transient
// failures (network, non-200) leave everything still queued for the
// next flush. A card-chunk failure aborts the WHOLE flush: sending
// reviews after an unacked card chunk would violate the cards-first
// ordering and bounce card_client_id reviews as unknown.
export async function flushOutbox() {
  if (syncDisabled) return {flushed: 0, created: 0, disabled: true};
  if (await guestAdoptionPending()) return {flushed: 0, created: 0, adoption_pending: true};
  let queued = await getAll("outbox_reviews");
  let localCards = await getAll("local_cards");
  queued = await parkCorruptRows("outbox_reviews", queued, "review");
  localCards = await parkCorruptRows("local_cards", localCards, "card");
  if (!queued.length && !localCards.length) return {flushed: 0, created: 0};

  // Resolve the session's identity before sending anything: the
  // outbox belongs to meta.owner, and only that account may sync it.
  const fetched = await fetchSnapshotPayload();
  if (!fetched.ok) return {flushed: 0, created: 0, status: fetched.status};
  if (!(await ownerAllows(fetched.snapshot.user))) {
    return {flushed: 0, created: 0, disabled: true};
  }

  const device = await metaGet("device");
  const deviceId = device && device.device_id ? device.device_id : null;

  // ---- cards first --------------------------------------------------
  localCards.sort((a, b) =>
    (a.created_at || "") < (b.created_at || "") ? -1 : (a.created_at || "") > (b.created_at || "") ? 1 : 0
  );
  let created = 0;
  let rejectedCards = 0;
  for (let start = 0; start < localCards.length; start += CARDS_PER_CHUNK) {
    const chunk = localCards.slice(start, start + CARDS_PER_CHUNK);
    const result = await postSyncChunk(deviceId, chunk.map(toWireCard), []);
    if (!result) {
      // Transient failure with card chunks (and all reviews) still
      // queued: report partial so the shell keeps its sync banner
      // instead of toasting success.
      return {flushed: 0, rejected: 0, created, rejectedCards, partial: true};
    }
    const byClientId = new Map((result.cards || []).map((c) => [c.client_id, c]));
    for (const item of chunk) {
      const itemResult = byClientId.get(item.client_id);
      if (!itemResult) continue; // unreported: leave queued
      if (itemResult.status === "created") {
        // The card's ladder overlay lives on the local_cards row
        // about to be deleted. Carry it to the cards store under the
        // new server identity and stamp any queued reviews with the
        // mapping (a local bookkeeping field; toWireReview never
        // sends it). Carrying EVERY created card (not only ones with
        // pending reviews) also keeps a card studyable when this
        // flush succeeds but the follow-up snapshot refresh fails
        // mid-blip; the next successful refresh replaces the row.
        // The whole ack runs under the store lock with a FRESH read
        // of outbox_reviews: a verdict tapped during the POST round
        // trip lands on the row via recordVerdict's own locked turn,
        // and processing against the stale pre-flush array would
        // delete that overlay unseen.
        await withLock(async () => {
          const row = await get("local_cards", item.client_id);
          const source = row || item;
          if (itemResult.question_id != null) {
            await put("cards", {
              question_id: itemResult.question_id,
              deck_id: source.deck_id ?? null,
              type: "short",
              prompt: source.prompt,
              answer: source.answer,
              local_step: source.local_step ?? null,
              local_next_due: source.local_next_due ?? null,
            });
            const queuedNow = await getAll("outbox_reviews");
            for (const r of queuedNow) {
              if (r.card_client_id === item.client_id) {
                await put("outbox_reviews", {...r, resolved_question_id: itemResult.question_id});
              }
            }
          }
          await remove("local_cards", item.client_id);
        });
        created += 1;
      } else if (itemResult.status === "rejected") {
        await put("rejects", {
          ...item,
          kind: "card",
          error: itemResult.error || "rejected",
          rejected_at: new Date().toISOString(),
        });
        await remove("local_cards", item.client_id);
        rejectedCards += 1;
      }
    }
  }

  // ---- reviews second ----------------------------------------------
  queued.sort((a, b) =>
    a.reviewed_at < b.reviewed_at ? -1 : a.reviewed_at > b.reviewed_at ? 1 : 0
  );
  let flushed = 0;
  let rejected = 0;
  for (let start = 0; start < queued.length; start += REVIEWS_PER_CHUNK) {
    const chunk = queued.slice(start, start + REVIEWS_PER_CHUNK);
    const result = await postSyncChunk(deviceId, [], chunk.map(toWireReview));
    if (!result) break; // transient failure: leave the rest queued
    const byClientId = new Map((result.reviews || []).map((r) => [r.client_id, r]));
    for (const item of chunk) {
      const itemResult = byClientId.get(item.client_id);
      if (!itemResult) continue; // unreported: leave queued
      if (itemResult.status === "rejected") {
        if (await awaitingLocalCard(item, itemResult)) continue;
        await put("rejects", {
          ...item,
          error: itemResult.error || "rejected",
          rejected_at: new Date().toISOString(),
        });
        await remove("outbox_reviews", item.client_id);
        rejected += 1;
      } else {
        await remove("outbox_reviews", item.client_id);
        flushed += 1;
      }
    }
  }
  return {flushed, rejected, created, rejectedCards};
}

// Minimal self-removing status toast (styled by components/offline.css,
// which the online stylesheet imports too). Lives here because both
// surfaces use it: init() below toasts after a background flush on
// online pages, and the offline shell's reconnect flow reuses it.
// Cosmetic only: failures are swallowed.
export function showToast(text) {
  try {
    const previous = document.querySelector(".offline-toast");
    if (previous) previous.remove();
    const node = document.createElement("div");
    node.className = "offline-toast";
    node.setAttribute("role", "status");
    node.textContent = text;
    document.body.appendChild(node);
    setTimeout(() => node.remove(), 4000);
  } catch (e) {
    // never let a toast break the page
  }
}

// ---- owner-mismatch confirm-then-wipe (M5) ---------------------------

// Tiny DOM helper for the dialog below. Data (display names, counts)
// only ever lands via textContent; nothing here touches innerHTML.
function makeEl(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// Clear every prep-offline store and reseed from the signed-in
// account's snapshot. The wipe itself is one IDB transaction
// (store.wipeAll) taken under the store lock so an in-flight flush
// or refresh cannot interleave; the reseed is a forced snapshot
// refresh under the new identity (which also re-stamps meta.owner).
export async function wipeAndReseed() {
  await withLock(() => wipeAll());
  syncDisabled = false;
  ownerConflict = null;
  return refreshSnapshot({force: true});
}

function conflictSummaryText(owner, serverUser) {
  const ownerName = owner.display_name || owner.user_id || "another account";
  const serverName = serverUser.display_name || serverUser.id || "this account";
  return (
    "This device holds offline study data for " + ownerName +
    ". You're signed in as " + serverName + "."
  );
}

function discardLineText(outboxCount, localCardCount) {
  const parts = [];
  if (outboxCount) {
    parts.push(outboxCount + (outboxCount === 1 ? " unsynced review" : " unsynced reviews"));
  }
  if (localCardCount) {
    parts.push(localCardCount + (localCardCount === 1 ? " unsynced new card" : " unsynced new cards"));
  }
  if (!parts.length) {
    return "Starting fresh replaces that snapshot with this account's cards.";
  }
  return (
    parts.join(" and ") +
    " from that account will be discarded if you start fresh."
  );
}

function showOwnerConflictDialog({owner, serverUser, outboxCount, localCardCount, onWiped}) {
  const previous = document.querySelector("dialog.offline-owner-dialog");
  if (previous) previous.remove();

  const dialog = document.createElement("dialog");
  dialog.className = "offline-owner-dialog";
  // Same data-dialog convention the online templates use; the
  // backdrop-close wiring is done inline below because this node is
  // created long after dialog.js's attachDeclarative pass ran.
  dialog.setAttribute("data-dialog", "");
  dialog.setAttribute("aria-labelledby", "offline-owner-title");

  const title = makeEl("h3", null, "Offline data from another account");
  title.id = "offline-owner-title";
  dialog.appendChild(title);
  dialog.appendChild(makeEl("p", null, conflictSummaryText(owner, serverUser)));
  dialog.appendChild(makeEl("p", null, discardLineText(outboxCount, localCardCount)));
  dialog.appendChild(
    makeEl(
      "p",
      "offline-owner-keep-note",
      "Keep leaves that data in place; offline study and sync stay off " +
        "for this account on this device."
    )
  );

  const actions = makeEl("div", "offline-owner-actions");
  const keepBtn = makeEl("button", null, "Keep");
  keepBtn.type = "button";
  const wipeBtn = makeEl("button", "danger", "Wipe and start fresh");
  wipeBtn.type = "button";
  actions.appendChild(keepBtn);
  actions.appendChild(wipeBtn);
  dialog.appendChild(actions);

  let busy = false;

  // Backdrop click closes, same signal dialog.js keys on: the event
  // target is the dialog element itself only when the click missed
  // every descendant. Esc is native to showModal(). Either way the
  // close records NOTHING -- no meta flag, no wipe -- so the dialog
  // simply re-prompts on the next page load. Undecided is not "keep".
  dialog.addEventListener("click", (e) => {
    if (e.target === dialog && !busy) dialog.close();
  });
  dialog.addEventListener("cancel", (e) => {
    if (busy) e.preventDefault();
  });
  dialog.addEventListener("close", () => dialog.remove());

  keepBtn.addEventListener("click", async () => {
    if (busy) return;
    busy = true;
    try {
      // The recorded choice suppresses re-prompts for THIS mismatched
      // account only; a different account signing in later prompts
      // again (its id will not match dismissed_user_id).
      await metaPut("owner_conflict", {
        dismissed_user_id: serverUser.id,
        dismissed_at: new Date().toISOString(),
      });
    } catch (e) {
      console.warn("offline owner-conflict keep failed:", e);
    } finally {
      busy = false;
      dialog.close();
    }
  });

  wipeBtn.addEventListener("click", async () => {
    if (busy) return;
    busy = true;
    wipeBtn.classList.add("is-loading");
    try {
      const result = await wipeAndReseed();
      dialog.close();
      showToast(
        result && result.ok
          ? "Offline data reset for this account"
          : "Offline data cleared. Cards will re-save next time prep loads online."
      );
      if (onWiped) await onWiped();
    } catch (e) {
      console.warn("offline owner-conflict wipe failed:", e);
      dialog.close();
      showToast("Could not reset offline data. Try again.");
    } finally {
      busy = false;
      wipeBtn.classList.remove("is-loading");
    }
  });

  document.body.appendChild(dialog);
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

// Surface the confirm-then-wipe dialog if (and only if) the owner
// guard tripped this session and the user has not already chosen
// "keep" for this same mismatched account. Returns true when the
// dialog was shown. Both surfaces call it: init() below after the
// online flush/refresh chain reports disabled, and the offline
// shell's reconnect flow (which passes onWiped to re-render from the
// freshly reseeded stores).
export async function maybeConfirmOwnerConflict({onWiped} = {}) {
  // An open dialog means the user is mid-decision (possibly mid-wipe);
  // a connectivity flap re-running the chain must not yank it.
  if (document.querySelector("dialog.offline-owner-dialog")) return;
  if (!ownerConflict) return false;
  try {
    const dismissed = await metaGet("owner_conflict");
    if (dismissed && dismissed.dismissed_user_id === ownerConflict.serverUser.id) {
      return false;
    }
    const [outbox, localCards] = await Promise.all([
      getAll("outbox_reviews"),
      getAll("local_cards"),
    ]);
    showOwnerConflictDialog({
      owner: ownerConflict.owner,
      serverUser: ownerConflict.serverUser,
      outboxCount: outbox.length,
      localCardCount: localCards.length,
      onWiped,
    });
    return true;
  } catch (e) {
    console.warn("offline owner-conflict prompt failed:", e);
    return false;
  }
}

// ---- guest adoption confirm ------------------------------------------

// Accept: open the gate for this flush and forced refresh, run the
// untouched sync machinery (cards first, then reviews, then the
// refresh that stamps meta.owner), and only then delete meta.guest.
// The deletion waits for the refresh to land: a mid-adoption failure
// keeps the guest record so the next load re-prompts with the same
// deck, and the retried flush is an idempotent replay.
async function adoptGuestData() {
  adoptionApproved = true;
  const flush = await flushOutbox();
  // A partial or transiently-failed flush stops BEFORE the refresh:
  // stamping meta.owner now would end state 2 with rows still queued,
  // so the honest outcome is a kept guest record and a re-prompt
  // whose retried flush is an idempotent replay.
  if (flush && (flush.partial || flush.status || flush.disabled)) {
    return {ok: false, flush, refresh: null};
  }
  const refresh = await refreshSnapshot({force: true});
  if (!(refresh && refresh.ok)) return {ok: false, flush, refresh};
  await withLock(() => remove("meta", "guest"));
  return {ok: true, flush, refresh};
}

// Discard: wipe the guest rows under the lock, then proceed as a
// fresh device (the seed refresh stamps the signed-in user as owner).
async function discardGuestData() {
  await withLock(async () => {
    await clear("local_cards");
    await clear("outbox_reviews");
    await clear("rejects");
    await remove("meta", "guest");
  });
  // The wipe above IS the discard. The reseed is best-effort (a
  // fresh device seeds on its next load anyway), so a reseed failure
  // must not surface as a failed discard.
  try {
    return await refreshSnapshot({force: true});
  } catch (e) {
    console.warn("post-discard reseed failed:", e);
    return {ok: false};
  }
}

function adoptionBodyText(guest, cardCount, reviewCount) {
  const name = (guest && guest.display_name) || "Your deck";
  return (
    name + ": " +
    cardCount + (cardCount === 1 ? " card" : " cards") + ", " +
    reviewCount + (reviewCount === 1 ? " review" : " reviews") +
    " from before you signed in."
  );
}

function showAdoptionDialog({guest, cardCount, reviewCount}) {
  const previous = document.querySelector("dialog.offline-adoption-dialog");
  if (previous) previous.remove();

  const dialog = document.createElement("dialog");
  // Shares the owner-dialog chrome classes (components/offline.css);
  // the second class is the adoption-specific handle.
  dialog.className = "offline-owner-dialog offline-adoption-dialog";
  dialog.setAttribute("data-dialog", "");
  dialog.setAttribute("aria-labelledby", "offline-adoption-title");

  const title = makeEl("h3", null, "Add your deck to this account?");
  title.id = "offline-adoption-title";
  dialog.appendChild(title);
  dialog.appendChild(makeEl("p", null, adoptionBodyText(guest, cardCount, reviewCount)));
  dialog.appendChild(
    makeEl(
      "p",
      null,
      "Add it to keep studying with this account. Discard removes it from this device."
    )
  );

  const actions = makeEl("div", "offline-owner-actions");
  const discardBtn = makeEl("button", "danger", "Discard it");
  discardBtn.type = "button";
  const acceptBtn = makeEl("button", null, "Add to my account");
  acceptBtn.type = "button";
  actions.appendChild(discardBtn);
  actions.appendChild(acceptBtn);
  dialog.appendChild(actions);

  let busy = false;

  // Backdrop click / Esc decides NOTHING: no flush, no owner stamp,
  // no wipe. The gate keeps holding and the dialog re-prompts on the
  // next authenticated load. Undecided is not consent (the same rule
  // the owner-conflict dialog pins for "keep").
  dialog.addEventListener("click", (e) => {
    if (e.target === dialog && !busy) dialog.close();
  });
  dialog.addEventListener("cancel", (e) => {
    if (busy) e.preventDefault();
  });
  dialog.addEventListener("close", () => dialog.remove());

  acceptBtn.addEventListener("click", async () => {
    if (busy) return;
    busy = true;
    acceptBtn.classList.add("is-loading");
    try {
      const result = await adoptGuestData();
      dialog.close();
      showToast(
        result.ok
          ? "Deck added to your account."
          : "Couldn't finish adding the deck. It will retry next time prep loads."
      );
    } catch (e) {
      console.warn("guest deck adoption failed:", e);
      dialog.close();
      showToast("Couldn't add the deck. Try again.");
    } finally {
      busy = false;
      acceptBtn.classList.remove("is-loading");
    }
  });

  discardBtn.addEventListener("click", async () => {
    if (busy) return;
    busy = true;
    discardBtn.classList.add("is-loading");
    try {
      await discardGuestData();
      dialog.close();
      showToast("Deck discarded.");
    } catch (e) {
      console.warn("guest deck discard failed:", e);
      dialog.close();
      showToast("Could not discard the deck. Try again.");
    } finally {
      busy = false;
      discardBtn.classList.remove("is-loading");
    }
  });

  document.body.appendChild(dialog);
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

// The adoption state table, evaluated once per page load. Owner-absent
// guest data (state 2, ADOPTABLE) holds the whole flush/refresh chain
// and shows the confirm when a signed-in user exists to adopt into;
// every other state proceeds through the normal chain unchanged. The
// snapshot fetch here is identity-only: nothing is written on this
// path.
async function initAdoption() {
  if (!(await guestAdoptionPending())) return {pending: false};
  const fetched = await fetchSnapshotPayload();
  if (!fetched.ok) return {pending: true, shown: false};
  if (!document.querySelector("dialog.offline-adoption-dialog")) {
    const [guest, localCards, outbox] = await Promise.all([
      metaGet("guest"),
      getAll("local_cards"),
      getAll("outbox_reviews"),
    ]);
    showAdoptionDialog({guest, cardCount: localCards.length, reviewCount: outbox.length});
  }
  return {pending: true, shown: true};
}

// Entry point for app.js on online pages. The adoption state table
// runs first: an adoptable device (owner absent, guest data present)
// holds the whole chain until the dialog decides. Otherwise flush
// first (a no-op while the outbox is empty), then refresh the
// snapshot -- forced after any flush that MOVED items, acked or
// rejected: acked reviews need the server's FSRS truth pulled down,
// and a rejected review means the server never rescheduled that card,
// so its local overlay must snap back to server state now rather than
// linger for up to the throttle window. The force matters doubly for
// created cards: their local_cards rows are deleted on ack and the
// refresh is what delivers them back as real snapshot cards. If the
// owner guard tripped anywhere in the chain, surface the
// confirm-then-wipe dialog (never silent, never automatic).
// Fire-and-forget: must never throw into the page, and must never
// block page behaviors.
export function init() {
  try {
    initAdoption()
      .then((adoption) => {
        if (adoption.pending) return null;
        return flushOutbox()
          .then((result) => {
            const bits = [];
            if (result && result.created) {
              bits.push(
                result.created === 1
                  ? "1 offline card added"
                  : result.created + " offline cards added"
              );
            }
            if (result && result.flushed) {
              bits.push(
                result.flushed === 1
                  ? "1 offline review synced"
                  : result.flushed + " offline reviews synced"
              );
            }
            if (bits.length) showToast(bits.join(", "));
            return refreshSnapshot({
              force: Boolean(
                result &&
                  (result.flushed || result.created || result.rejected || result.rejectedCards)
              ),
            });
          })
          .then(() => maybeConfirmOwnerConflict());
      })
      .catch((e) => {
        console.warn("offline sync failed:", e);
      });
  } catch (e) {
    console.warn("offline sync init failed:", e);
  }
}
