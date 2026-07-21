// sync.js: snapshot refresh (and, later, outbox flush). The one
// shared seam between the online app and the offline shell: app.js
// imports it on every online page (via init below); the offline app
// only reads what it wrote.
//
// M1 scope: refreshSnapshot only. The outbox flush ships with the M2
// server endpoint + M3 study surface (docs/OFFLINE.md section 8).

import {bulkReplace, metaGet, metaPut} from "./store.js";

const REFRESH_INTERVAL_MS = 60 * 60 * 1000;

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

// Fetch GET /api/offline/snapshot and fully replace the local decks +
// cards stores, then stamp meta.owner. Returns a small result object;
// never intended to run in a user-blocking path.
//
// Throttled: skipped when the last successful refresh is younger than
// an hour, unless force is set (a post-flush refresh will force).
export async function refreshSnapshot({force = false} = {}) {
  if (!force) {
    const sync = await metaGet("sync");
    if (sync && sync.last_refresh_at) {
      const age = Date.now() - Date.parse(sync.last_refresh_at);
      if (Number.isFinite(age) && age >= 0 && age < REFRESH_INTERVAL_MS) {
        return {ok: true, skipped: true};
      }
    }
  }

  const response = await fetch(ROOT_PATH + "/api/offline/snapshot", {
    credentials: "same-origin",
    headers: {accept: "application/json"},
  });
  // Anything but a clean 200 (signed-out 401, transient 5xx) leaves
  // the local snapshot untouched; it stays disposable and re-fetchable.
  if (response.status !== 200) return {ok: false, status: response.status};
  const snapshot = await response.json();
  if (!snapshot || !snapshot.user || !snapshot.user.id) {
    return {ok: false, status: 200, malformed: true};
  }

  // Full replace sidesteps tombstone bookkeeping for deletions. Local
  // overlay fields are seeded null; M3's refresh will preserve them
  // for cards with queued reviews (no outbox exists yet in M1).
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

// M1 stub: the outbox flush (POST /api/offline/sync, chunked, with
// the owner-mismatch guard) arrives with M2 (server endpoint) + M3
// (study surface that writes the outbox). Until then nothing is ever
// queued, so there is nothing to send.
export async function flushOutbox() {
  return {flushed: 0};
}

// Entry point for app.js on online pages. Fire-and-forget: must never
// throw into the page, and must never block page behaviors.
export function init() {
  try {
    refreshSnapshot().catch((e) => {
      console.warn("offline snapshot refresh failed:", e);
    });
  } catch (e) {
    console.warn("offline sync init failed:", e);
  }
}
