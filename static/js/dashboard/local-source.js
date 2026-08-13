// local-source.js: the DeckSource adapter over the offline IndexedDB
// stores. Split from source.js because it imports the offline store
// and the study scheduler: the signed-in dashboard needs only
// ServerSource, and pulling this chain onto its critical path costs a
// waterfall level on the app's most-trafficked page.
//
// Same contract as ServerSource (see source.js). What differs is what
// this surface can answer: `unsynced` is always a count, because a
// device with an outbox knows.

import {getAll, metaGet} from "../offline/store.js";
import {isDueNow, nextDueInMinutes} from "../study/source.js";
import {deckLabel, shapeDeck} from "./source.js";

// The server's deck order (DeckRepo.list_summaries): pinned first,
// most recently pinned leading, then by label. Sorted here because
// IndexedDB hands rows back in key order, which is neither.
// Comparison is codepoint order, matching sqlite's default collation.
function byServerOrder(a, b) {
  const ap = a.pinned_at || "";
  const bp = b.pinned_at || "";
  if (ap !== bp) return ap && bp ? (ap < bp ? 1 : -1) : ap ? -1 : 1;
  const al = deckLabel(a);
  const bl = deckLabel(b);
  return al < bl ? -1 : al > bl ? 1 : 0;
}

// The overview an offline device can state, from rows already read.
// Pure: the adapter below owns the reads, this owns the arithmetic.
export function shapeLocalOverview({owner, decks, cards, localCards, outbox}, now = Date.now()) {
  const all = (cards || []).concat(localCards || []);
  const byDeck = new Map();
  const authoredByDeck = new Map();
  for (const card of all) {
    if (card.deck_id === null || card.deck_id === undefined) continue;
    const entry = byDeck.get(card.deck_id) || {due: 0, total: 0};
    entry.total += 1;
    if (isDueNow(card, now)) entry.due += 1;
    byDeck.set(card.deck_id, entry);
  }
  for (const card of localCards || []) {
    if (card.deck_id === null || card.deck_id === undefined) continue;
    authoredByDeck.set(card.deck_id, (authoredByDeck.get(card.deck_id) || 0) + 1);
  }
  const rows = (decks || []).slice().sort(byServerOrder);
  return {
    user: owner
      ? {
          display_name: owner.display_name || null,
          is_anonymous: Boolean(owner.is_anonymous),
        }
      : null,
    decks: rows.map((deck) => {
      const held = byDeck.get(deck.id) || {due: 0, total: 0};
      // "in deck" counts questions on both surfaces, so the row uses
      // the snapshot's count: this device holds no card for a
      // suspended question and would otherwise state a smaller number
      // under the same label. Cards authored here are not on the
      // server yet, so they add. A snapshot written before the field
      // existed falls back to what is held.
      const total = Number.isFinite(deck.total)
        ? deck.total + (authoredByDeck.get(deck.id) || 0)
        : held.total;
      return shapeDeck(deck, {due: held.due, total});
    }),
    due: all.filter((card) => isDueNow(card, now)).length,
    total: all.length,
    nextDueMinutes: nextDueInMinutes(all),
    unsynced: {reviews: (outbox || []).length, cards: (localCards || []).length},
  };
}

// Every store the overview draws from, read together so the counts
// describe one instant rather than a walk across several.
async function readLocal() {
  const [owner, decks, cards, localCards, outbox] = await Promise.all([
    metaGet("owner"),
    getAll("decks"),
    getAll("cards"),
    getAll("local_cards"),
    getAll("outbox_reviews"),
  ]);
  return {owner, decks, cards, localCards, outbox};
}

// DeckSource over the offline stores. Re-reads on every call: the
// study loop writes overlays and outbox rows behind its back, so a
// cached shape would render stale counts after a sitting.
export class LocalSource {
  constructor({read = readLocal} = {}) {
    this._read = read;
  }

  async overview() {
    return shapeLocalOverview(await this._read());
  }
}
