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

import {get, getAll, bulkReplace, metaGet, metaPut, put, remove, wipeAll, withLock} from "./store.js";

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
  return {
    client_id: item.client_id,
    deck_id: item.deck_id ?? null,
    prompt: item.prompt,
    answer: item.answer,
    created_at: item.created_at,
  };
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

// Entry point for app.js on online pages. Flush first (a no-op while
// the outbox is empty), then refresh the snapshot -- forced after any
// flush that MOVED items, acked or rejected: acked reviews need the
// server's FSRS truth pulled down, and a rejected review means the
// server never rescheduled that card, so its local overlay must snap
// back to server state now rather than linger for up to the throttle
// window. The force matters doubly for created cards: their
// local_cards rows are deleted on ack and the refresh is what
// delivers them back as real snapshot cards. If the owner guard
// tripped anywhere in the chain, surface the confirm-then-wipe
// dialog (never silent, never automatic). Fire-and-forget: must
// never throw into the page, and must never block page behaviors.
export function init() {
  try {
    flushOutbox()
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
      .then(() => maybeConfirmOwnerConflict())
      .catch((e) => {
        console.warn("offline sync failed:", e);
      });
  } catch (e) {
    console.warn("offline sync init failed:", e);
  }
}
