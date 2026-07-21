// store.js: the IndexedDB layer for offline study. The only module
// that touches IDB (docs/OFFLINE.md section 3, "IndexedDB schema").
//
// One database, `prep-offline`, schema version 1. The whole database
// belongs to one owner at a time (meta.owner); it is wiped on owner
// change rather than composite-keyed per user.
//
// Object stores:
//
//   meta            out-of-line string keys. Records:
//                     "owner"  {user_id, display_name, snapshot_at, build}
//                     "device" {device_id}  (UUID minted on first open)
//                     "sync"   {last_refresh_at}  (throttle bookkeeping)
//   decks           keyPath "id"           {id, name, display_name}
//   cards           keyPath "question_id"  snapshot card + local overlay
//                                          fields {local_step, local_next_due}
//   local_cards     keyPath "client_id"    written from M4 (authoring)
//   outbox_reviews  keyPath "client_id"    written from M3 (study);
//                                          index "reviewed_at"; drained
//                                          by sync.js flushOutbox
//   rejects         keyPath "client_id"    permanent server rejects,
//                                          written by sync.js flushOutbox

const DB_NAME = "prep-offline";
const DB_VERSION = 1;

let dbPromise = null;

function uuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  // Secure-context fallback; RFC 4122 v4 from raw random bytes.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return (
    hex.slice(0, 8) + "-" + hex.slice(8, 12) + "-" + hex.slice(12, 16) +
    "-" + hex.slice(16, 20) + "-" + hex.slice(20)
  );
}

function promisify(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function txDone(tx) {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error || new Error("transaction aborted"));
  });
}

function openDb() {
  if (dbPromise) return dbPromise;
  dbPromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains("meta")) {
        db.createObjectStore("meta");
      }
      if (!db.objectStoreNames.contains("decks")) {
        db.createObjectStore("decks", {keyPath: "id"});
      }
      if (!db.objectStoreNames.contains("cards")) {
        db.createObjectStore("cards", {keyPath: "question_id"});
      }
      if (!db.objectStoreNames.contains("local_cards")) {
        db.createObjectStore("local_cards", {keyPath: "client_id"});
      }
      if (!db.objectStoreNames.contains("outbox_reviews")) {
        const outbox = db.createObjectStore("outbox_reviews", {keyPath: "client_id"});
        outbox.createIndex("reviewed_at", "reviewed_at");
      }
      if (!db.objectStoreNames.contains("rejects")) {
        db.createObjectStore("rejects", {keyPath: "client_id"});
      }
    };
    request.onsuccess = async () => {
      const db = request.result;
      db.onversionchange = () => db.close();
      try {
        await ensureDeviceRecord(db);
      } catch (e) {
        // A missing device record must not block reads; M1 never
        // sends it anywhere. Minting retries on the next open.
      }
      resolve(db);
    };
    request.onerror = () => reject(request.error);
  });
  return dbPromise;
}

async function ensureDeviceRecord(db) {
  const existing = await promisify(
    db.transaction("meta", "readonly").objectStore("meta").get("device")
  );
  if (existing && existing.device_id) return;
  const tx = db.transaction("meta", "readwrite");
  tx.objectStore("meta").put({device_id: uuid()}, "device");
  await txDone(tx);
}

// ---- generic helpers -------------------------------------------------

export async function get(storeName, key) {
  const db = await openDb();
  const result = await promisify(
    db.transaction(storeName, "readonly").objectStore(storeName).get(key)
  );
  return result === undefined ? null : result;
}

export async function getAll(storeName) {
  const db = await openDb();
  return promisify(
    db.transaction(storeName, "readonly").objectStore(storeName).getAll()
  );
}

export async function put(storeName, value, key) {
  const db = await openDb();
  const tx = db.transaction(storeName, "readwrite");
  // Out-of-line-key stores (meta) take an explicit key; keyPath stores
  // must not be given one.
  if (key === undefined) tx.objectStore(storeName).put(value);
  else tx.objectStore(storeName).put(value, key);
  await txDone(tx);
}

export async function remove(storeName, key) {
  const db = await openDb();
  const tx = db.transaction(storeName, "readwrite");
  tx.objectStore(storeName).delete(key);
  await txDone(tx);
}

export async function clear(storeName) {
  const db = await openDb();
  const tx = db.transaction(storeName, "readwrite");
  tx.objectStore(storeName).clear();
  await txDone(tx);
}

// Full replace: clear + put every record inside ONE transaction, so a
// crash mid-write can never leave a half-empty store (the transaction
// either commits whole or rolls back whole).
export async function bulkReplace(storeName, records) {
  const db = await openDb();
  const tx = db.transaction(storeName, "readwrite");
  const os = tx.objectStore(storeName);
  os.clear();
  for (const record of records) os.put(record);
  await txDone(tx);
}

// ---- meta convenience ------------------------------------------------

export function metaGet(name) {
  return get("meta", name);
}

export function metaPut(name, value) {
  return put("meta", value, name);
}
