// sync.js: snapshot refresh + outbox flush. The one shared seam
// between the online app and the offline shell: app.js imports it on
// every online page (via init below); the offline app only reads what
// it wrote.
//
// M2 scope: the flush is real (reads outbox_reviews, POSTs batches to
// /api/offline/sync, prunes acked items, records rejects) but vacuous
// in practice until M3's study surface writes to the outbox. The
// owner-mismatch guard ships with the flush, not after it: syncing
// under a mismatched sign-in would replay one account's outbox into
// another. On mismatch, sync disables entirely for the session -- no
// flush, no snapshot write (the confirm-then-wipe UX arrives in M5).

import {get, getAll, bulkReplace, metaGet, metaPut, put, remove} from "./store.js";

const REFRESH_INTERVAL_MS = 60 * 60 * 1000;

// Server cap is 500 reviews per request; the client chunks under it,
// in reviewed_at order (docs/OFFLINE.md section 4).
const REVIEWS_PER_CHUNK = 500;

// Flipped on owner mismatch; every sync entry point then refuses to
// touch the network or the local stores until the next page load.
let syncDisabled = false;

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
async function ownerAllows(serverUserId) {
  const owner = await metaGet("owner");
  if (owner && owner.user_id && serverUserId && owner.user_id !== serverUserId) {
    syncDisabled = true;
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
    if (sync && sync.last_refresh_at) {
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
  if (!(await ownerAllows(snapshot.user.id))) return {ok: false, disabled: true};

  // Full replace sidesteps tombstone bookkeeping for deletions. Local
  // overlay fields are seeded null; M3's refresh will preserve them
  // for cards with queued reviews (nothing writes the outbox yet).
  await bulkReplace("decks", snapshot.decks || []);
  await bulkReplace(
    "cards",
    (snapshot.cards || []).map((card) => ({
      ...card,
      local_step: null,
      local_next_due: null,
    }))
  );
  await metaPut("owner", {
    user_id: snapshot.user.id,
    display_name: snapshot.user.display_name || "",
    snapshot_at: snapshot.generated_at || new Date().toISOString(),
    build: buildToken(),
  });
  await metaPut("sync", {last_refresh_at: new Date().toISOString()});
  return {ok: true};
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

// Flush the outbox: POST queued reviews in reviewed_at order, in
// chunks under the server cap. Acked items (applied /
// logged_no_reschedule / created) leave the outbox; permanent
// rejects move to the rejects store for the needs-attention list;
// transient failures (network, non-200) leave everything still
// queued for the next flush -- the server's idempotency table makes
// the retry a pure replay.
//
// Vacuous until M3 writes outbox_reviews (and M4 sends new_cards);
// the wiring below is the real thing regardless.
export async function flushOutbox() {
  if (syncDisabled) return {flushed: 0, disabled: true};
  let queued = await getAll("outbox_reviews");
  // Pre-validate: a row whose client_id is not a string cannot be
  // matched against the server's echoed results (IDB keys may be
  // numbers), so it would be re-sent and re-rejected on every flush
  // forever. Park corrupt rows in rejects before POSTing anything.
  const corrupt = queued.filter((r) => typeof r.client_id !== "string" || !r.client_id);
  for (const row of corrupt) {
    await put("rejects", {
      client_id: String(row.client_id ?? "corrupt-" + Date.now()),
      kind: "review",
      error: "corrupt outbox row (non-string client_id)",
      item: row,
    });
    await remove("outbox_reviews", row.client_id);
  }
  if (corrupt.length) queued = queued.filter((r) => typeof r.client_id === "string" && r.client_id);
  if (!queued.length) return {flushed: 0};

  // Resolve the session's identity before sending anything: the
  // outbox belongs to meta.owner, and only that account may sync it.
  const fetched = await fetchSnapshotPayload();
  if (!fetched.ok) return {flushed: 0, status: fetched.status};
  if (!(await ownerAllows(fetched.snapshot.user.id))) {
    return {flushed: 0, disabled: true};
  }

  queued.sort((a, b) =>
    a.reviewed_at < b.reviewed_at ? -1 : a.reviewed_at > b.reviewed_at ? 1 : 0
  );
  const device = await metaGet("device");

  let flushed = 0;
  let rejected = 0;
  for (let start = 0; start < queued.length; start += REVIEWS_PER_CHUNK) {
    const chunk = queued.slice(start, start + REVIEWS_PER_CHUNK);
    let response;
    try {
      response = await fetch(ROOT_PATH + "/api/offline/sync", {
        method: "POST",
        credentials: "same-origin",
        headers: {"content-type": "application/json", accept: "application/json"},
        body: JSON.stringify({
          device_id: device && device.device_id ? device.device_id : null,
          new_cards: [],
          reviews: chunk.map(toWireReview),
        }),
      });
    } catch (e) {
      break; // network flap: leave the rest queued
    }
    if (response.status !== 200) break; // transient server error: leave queued
    const result = await response.json();
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
  return {flushed, rejected};
}

// Entry point for app.js on online pages. Flush first (a no-op while
// the outbox is empty), then refresh the snapshot -- forced after a
// real flush so local SRS state converges to the server's FSRS truth
// immediately. Fire-and-forget: must never throw into the page, and
// must never block page behaviors.
export function init() {
  try {
    flushOutbox()
      .then((result) => refreshSnapshot({force: Boolean(result && result.flushed)}))
      .catch((e) => {
        console.warn("offline sync failed:", e);
      });
  } catch (e) {
    console.warn("offline sync init failed:", e);
  }
}
