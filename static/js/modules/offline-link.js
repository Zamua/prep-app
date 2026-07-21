// Reveals the landing page's hidden "study offline" link when this
// device already holds an offline snapshot (docs/OFFLINE.md section
// 3: the landing page gets the link "when a snapshot exists"). The
// check is client-side because the landing render is anonymous: the
// server cannot know what this device has cached.
//
// Lazy-imported by app.js only when the [data-offline-link] hook is
// on the page, so non-landing pages never load this module.
//
// The existence probe prefers indexedDB.databases(): a bare open()
// would CREATE an empty prep-offline database for every first-time
// visitor just to discover there is nothing in it. Engines without
// databases() fall through to the open-based read; the empty
// database that leaves behind is harmless (store.js creates the
// same one the moment the user ever syncs).

export async function init(node) {
  try {
    if (!node || !("indexedDB" in window)) return;
    if (indexedDB.databases) {
      const existing = await indexedDB.databases();
      if (!existing.some((db) => db.name === "prep-offline")) return;
    }
    const store = await import("@/offline/store.js");
    const owner = await store.metaGet("owner");
    if (owner && owner.user_id) node.hidden = false;
  } catch (e) {
    // Reveal-only affordance; the landing page must never break over
    // a storage quirk. Staying hidden is the safe failure mode.
  }
}
