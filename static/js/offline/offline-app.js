// offline-app.js: bootstrap for the /offline shell. Client-rendered
// on purpose: the shell is served from the SW cache with no network
// and no server-resolved user, so everything here reads IndexedDB
// (docs/OFFLINE.md section 3).
//
// M1 scope: read-only. Owner line, per-deck card + due counts, the
// due-now list (prompts only), and an honest empty state when no
// snapshot has ever been seeded. Study/authoring views arrive in M3/M4.
//
// Plain DOM building, no framework, no innerHTML for data (card
// prompts and deck names are user content; textContent only).

import {getAll, metaGet} from "./store.js";

// A card's effective due time offline: the local overlay when set
// (never set in M1), else the snapshot's server-computed next_due.
function effectiveDue(card) {
  return card.local_next_due || card.next_due || null;
}

// Due now = effective due parses and is in the past. A null due (a
// card the server considers due immediately) counts as due; an
// unparseable timestamp does not (junk should not flood the queue).
function isDueNow(card, now) {
  const due = effectiveDue(card);
  if (due === null) return true;
  const t = Date.parse(due);
  if (!Number.isFinite(t)) return false;
  return t <= now;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// The prelude pattern shared with the online settings pages: eyebrow,
// display headline (with an italic beat), lede.
function prelude(eyebrowText, headStart, headEm, ledeText) {
  const section = el("section", "prelude");
  section.appendChild(el("p", "eyebrow", eyebrowText));
  const h1 = el("h1", "display", headStart + " ");
  h1.appendChild(el("em", null, headEm));
  h1.appendChild(document.createTextNode("."));
  section.appendChild(h1);
  section.appendChild(el("p", "lede", ledeText));
  return section;
}

function sectionEyebrow(label) {
  const p = el("p", "section-eyebrow");
  p.appendChild(el("span", null, label));
  p.appendChild(el("span", "rule"));
  return p;
}

function renderEmpty(root) {
  root.replaceChildren(
    prelude(
      "Offline study",
      "Nothing cached",
      "yet",
      "Open prep while online first. Your decks and due cards are " +
        "saved to this device automatically, and this screen works " +
        "without a connection after that."
    )
  );
}

function renderSnapshot(root, owner, decks, cards) {
  const now = Date.now();
  // Oldest-due first. A null due means "due immediately"; sort those
  // to the front (epoch 0) so they surface before dated backlog.
  const dueTime = (card) => {
    const due = effectiveDue(card);
    if (due === null) return 0;
    const t = Date.parse(due);
    return Number.isFinite(t) ? t : 0;
  };
  const dueCards = cards
    .filter((card) => isDueNow(card, now))
    .sort((a, b) => dueTime(a) - dueTime(b));

  const frag = document.createDocumentFragment();

  // ---- prelude + owner line -----------------------------------------
  const who = owner.display_name || "you";
  const lede =
    "Studying as " + who + ". " +
    (dueCards.length
      ? dueCards.length + (dueCards.length === 1 ? " card is" : " cards are") +
        " due right now."
      : "Nothing is due right now.") +
    " Offline study is read-only for the moment.";
  frag.appendChild(prelude("Offline study", "Your cards,", "offline", lede));

  // ---- per-deck counts ----------------------------------------------
  const deckSection = el("section", "offline-decks");
  deckSection.appendChild(sectionEyebrow("Decks"));
  if (decks.length === 0) {
    deckSection.appendChild(el("p", "muted", "No decks in the snapshot."));
  } else {
    const byDeck = new Map();
    for (const card of cards) {
      const entry = byDeck.get(card.deck_id) || {total: 0, due: 0};
      entry.total += 1;
      if (isDueNow(card, now)) entry.due += 1;
      byDeck.set(card.deck_id, entry);
    }
    const list = el("ul", "offline-deck-list");
    for (const deck of decks) {
      const counts = byDeck.get(deck.id) || {total: 0, due: 0};
      const item = el("li", "offline-deck");
      item.appendChild(el("span", "offline-deck-name", deck.display_name || deck.name));
      item.appendChild(
        el(
          "span",
          "offline-deck-counts muted",
          counts.due + " due · " + counts.total +
            (counts.total === 1 ? " card" : " cards")
        )
      );
      list.appendChild(item);
    }
    deckSection.appendChild(list);
  }
  frag.appendChild(deckSection);

  // ---- due-now list (prompts only in M1) ----------------------------
  const dueSection = el("section", "offline-due");
  dueSection.appendChild(sectionEyebrow("Due now"));
  if (dueCards.length === 0) {
    dueSection.appendChild(
      el("p", "muted", "Nothing due. Check back later, or come back online to sync.")
    );
  } else {
    const list = el("ul", "offline-due-list");
    for (const card of dueCards) {
      list.appendChild(el("li", "offline-due-card", card.prompt || ""));
    }
    dueSection.appendChild(list);
  }
  frag.appendChild(dueSection);

  // ---- footer line --------------------------------------------------
  if (owner.snapshot_at) {
    const stamp = Date.parse(owner.snapshot_at);
    const label = Number.isFinite(stamp)
      ? new Date(stamp).toLocaleString()
      : owner.snapshot_at;
    frag.appendChild(el("p", "muted offline-snapshot-stamp", "Snapshot from " + label + "."));
  }

  root.replaceChildren(frag);
}

async function boot() {
  const root = document.getElementById("offline-root") || document.body;
  try {
    const [owner, decks, cards] = await Promise.all([
      metaGet("owner"),
      getAll("decks"),
      getAll("cards"),
    ]);
    if (!owner) {
      renderEmpty(root);
      return;
    }
    renderSnapshot(root, owner, decks, cards);
  } catch (e) {
    // IndexedDB unavailable (private-mode quirks, storage wiped mid
    // read). Degrade to the honest empty state rather than a blank page.
    console.warn("offline app failed to read local data:", e);
    renderEmpty(root);
  }
}

boot();
