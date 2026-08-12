// instant-start.js: the landing hero's anonymous generation loop
// (docs/INSTANT-START.md sections 2 + 3.3). Four form states: idle,
// generating, ready, error. Generated cards land in the same
// IndexedDB the offline app studies from; `meta.guest` is written
// ONLY when `meta.owner` is absent (on an owner-present device the
// cards are ordinary authored rows that ride the normal sync flush).
//
// Lazy-imported by app.js on the [data-instant-start] hook, so only
// the instant landing pays for it. Card and deck text is model
// output: textContent only, never innerHTML.

// The deploy's root path, derived from this module's own URL (same
// trick as offline/sync.js).
const ROOT_PATH = new URL(import.meta.url).pathname.replace(/\/static\/js\/.*$/, "");

// Slightly above the server's own 60s generation cap, so the server's
// error shape wins the race when it can.
const GENERATE_TIMEOUT_MS = 75000;

let inFlight = false;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// Existence probe before touching store.js: a bare open() would
// CREATE an empty prep-offline database for every first-time visitor
// (same rationale as offline-link.js). Returns null when there is
// nothing to read.
async function readGuestState() {
  if (!("indexedDB" in window)) return null;
  if (indexedDB.databases) {
    const existing = await indexedDB.databases();
    if (!existing.some((db) => db.name === "prep-offline")) return null;
  }
  const store = await import("@/offline/store.js");
  const [owner, guest, cards] = await Promise.all([
    store.metaGet("owner"),
    store.metaGet("guest"),
    store.getAll("local_cards"),
  ]);
  return {owner, guest, cards};
}

export async function init(node) {
  const form = node.querySelector("[data-instant-form]");
  const textarea = form ? form.querySelector("textarea") : null;
  const button = form ? form.querySelector(".instant-generate") : null;
  const statusLine = node.querySelector("[data-instant-status]");
  const errorLine = node.querySelector("[data-instant-error]");
  const readyPanel = node.querySelector("[data-instant-ready]");
  const continueStrip = node.querySelector("[data-instant-continue]");
  if (!form || !textarea || !button || !statusLine || !errorLine || !readyPanel) return;
  const signInUrl = node.dataset.signInUrl || "";

  // Idle: enable the button now that the module is live. The
  // is-loading spinner slot is CSS-reserved, so the box never resizes.
  button.disabled = false;

  renderContinueStrip();

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submit();
  });

  // Returning-visitor strip. The owner-absent condition is
  // load-bearing: owner-present devices never write meta.guest, and
  // stale guest metadata must never surface on a device whose cards
  // sync through the normal owner flush.
  async function renderContinueStrip() {
    if (!continueStrip) return;
    try {
      const state = await readGuestState();
      if (!state || state.owner || !state.guest) return;
      const scheduler = await import("@/offline/scheduler.js");
      const now = Date.now();
      const total = state.cards.length;
      const due = state.cards.filter((c) => scheduler.due(now, c.local_next_due || null)).length;
      const link = document.createElement("a");
      link.className = "instant-continue-card";
      link.href = ROOT_PATH + "/offline";
      link.appendChild(el("span", "instant-continue-eyebrow", "Continue studying"));
      link.appendChild(
        el("span", "instant-continue-name", state.guest.display_name || "Your deck")
      );
      link.appendChild(
        el(
          "span",
          "instant-continue-counts",
          total + (total === 1 ? " card" : " cards") + " · " + due + " due"
        )
      );
      continueStrip.replaceChildren(link);
      continueStrip.hidden = false;
    } catch (e) {
      // Reveal-only affordance; staying hidden is the safe failure mode.
    }
  }

  async function submit() {
    // The guard must close before the first await: two submit events
    // landing in one task (double-tap, Enter plus click) would both
    // pass it and double-POST otherwise.
    if (inFlight) return;
    inFlight = true;
    try {
      const topic = textarea.value.trim();
      if (!topic) {
        textarea.focus();
        return;
      }

      // Replace-confirm BEFORE any spend: one guest deck per device.
      // Owner-absent local cards without meta.guest are still guest
      // data (the adoption gate counts them), so they get the same
      // confirm rather than a silent wipe.
      let guestState = null;
      try {
        guestState = await readGuestState();
      } catch (e) {
        guestState = null;
      }
      if (guestState && !guestState.owner && (guestState.guest || guestState.cards.length > 0)) {
        const name =
          (guestState.guest && guestState.guest.display_name) || "your saved cards";
        const ok = window.confirm(
          "Replace your current deck (" + name + ")? It's only stored on this device."
        );
        if (!ok) return;
      }

      button.classList.add("is-loading");
      errorLine.hidden = true;
      statusLine.textContent = "Writing your cards. Usually 10 to 20 seconds.";
      statusLine.hidden = false;
      try {
        let response = null;
        let body = null;
        try {
          response = await fetch(ROOT_PATH + "/api/instant/generate", {
            method: "POST",
            headers: {"content-type": "application/json", accept: "application/json"},
            body: JSON.stringify({topic}),
            signal: AbortSignal.timeout ? AbortSignal.timeout(GENERATE_TIMEOUT_MS) : undefined,
          });
          try {
            body = await response.json();
          } catch (e) {
            body = null;
          }
        } catch (e) {
          response = null; // network failure / timeout
        }
        if (!response || !response.ok || !body || !Array.isArray(body.cards)) {
          showError(response, body);
          return; // input preserved
        }
        let saved = true;
        try {
          await writeDeck(body, topic);
        } catch (e) {
          console.warn("instant deck could not be saved on this device:", e);
          saved = false;
        }
        renderReady(body, saved);
      } finally {
        button.classList.remove("is-loading");
        statusLine.hidden = true;
      }
    } finally {
      inFlight = false;
    }
  }

  // Everything under one lock turn, with FRESH owner/guest reads: the
  // confirm above is UX, but the replace/stamp decision must come
  // from the same instant as the writes.
  async function writeDeck(payload, topic) {
    const store = await import("@/offline/store.js");
    const nowIso = new Date().toISOString();
    await store.withLock(async () => {
      const owner = await store.metaGet("owner");
      if (!owner) {
        // Guest meta goes first: each put/remove is its own IDB
        // transaction, and a crash mid-replace must not leave
        // metadata naming a deck whose cards are gone (guest mode
        // derives the name from local_cards until the new record
        // lands).
        await store.remove("meta", "guest");
        const existing = await store.getAll("local_cards");
        const replaced = new Set(existing.map((c) => c.client_id));
        for (const card of existing) await store.remove("local_cards", card.client_id);
        if (replaced.size) {
          // An orphan review whose card no longer exists would
          // surface as a reject at adoption time.
          const reviews = await store.getAll("outbox_reviews");
          for (const review of reviews) {
            if (review.card_client_id && replaced.has(review.card_client_id)) {
              await store.remove("outbox_reviews", review.client_id);
            }
          }
        }
      }
      for (const card of payload.cards) {
        await store.put("local_cards", {
          client_id: store.uuid(),
          deck_id: null,
          deck_name: payload.display_name,
          type: "short",
          prompt: card.prompt,
          answer: card.answer,
          answer_regex: typeof card.answer_regex === "string" ? card.answer_regex : null,
          created_at: nowIso,
          local_step: 0,
          local_next_due: null,
        });
      }
      if (!owner) {
        await store.metaPut("guest", {
          deck_client_id: store.uuid(),
          display_name: payload.display_name,
          topic,
          created_at: nowIso,
        });
      }
    });
  }

  function showError(response, body) {
    errorLine.replaceChildren();
    const kind = body && body.kind;
    if (kind === "rate_limited" && body.scope === "day" && signInUrl) {
      errorLine.appendChild(document.createTextNode("You've reached today's limit. "));
      const a = document.createElement("a");
      a.href = signInUrl;
      a.textContent = "Create a free account";
      errorLine.appendChild(a);
      errorLine.appendChild(document.createTextNode(" to keep going."));
    } else {
      errorLine.textContent = errorText(kind, body);
    }
    errorLine.hidden = false;
  }

  function errorText(kind, body) {
    if (kind === "rate_limited") {
      if (body.scope === "day") {
        return "You've reached today's limit. Create a free account to keep going.";
      }
      return "One deck a minute. Try again shortly.";
    }
    if (kind === "busy") return "The free AI is busy right now. Try again in a few minutes.";
    if (kind === "invalid_topic") {
      return (body && typeof body.message === "string" && body.message) ||
        "Describe your topic in 1 to 500 characters.";
    }
    if (kind === "not_configured") {
      return (body && typeof body.message === "string" && body.message) ||
        "Instant decks aren't available right now.";
    }
    // generation_failed, network failure, timeout, unknown shapes
    return "That didn't work. Try again.";
  }

  function renderReady(payload, saved) {
    form.hidden = true;
    errorLine.hidden = true;
    if (continueStrip) continueStrip.hidden = true;
    readyPanel.replaceChildren();
    const n = payload.cards.length;
    readyPanel.appendChild(
      el(
        "h2",
        "instant-ready-heading",
        "Your deck: " + payload.display_name + ", " + n + (n === 1 ? " card" : " cards")
      )
    );
    if (saved) {
      const list = el("ul", "instant-preview");
      for (const card of payload.cards.slice(0, 3)) {
        list.appendChild(el("li", "instant-preview-prompt", card.prompt));
      }
      readyPanel.appendChild(list);
      const actions = el("div", "instant-actions");
      const start = document.createElement("a");
      start.className = "btn btn-primary";
      start.href = ROOT_PATH + "/offline";
      start.textContent = "Start studying";
      actions.appendChild(start);
      readyPanel.appendChild(actions);
    } else {
      // IndexedDB write failure: the deck still renders, read-only
      // from memory, and the account CTA replaces Start studying.
      const list = el("ul", "instant-preview instant-preview-full");
      for (const card of payload.cards) {
        const item = el("li", "instant-preview-card");
        item.appendChild(el("p", "instant-preview-prompt", card.prompt));
        item.appendChild(el("p", "instant-preview-answer", card.answer));
        list.appendChild(item);
      }
      readyPanel.appendChild(list);
      const note = el("p", "instant-save-error");
      note.appendChild(document.createTextNode("Couldn't save on this device. "));
      if (signInUrl) {
        const a = document.createElement("a");
        a.href = signInUrl;
        a.textContent = "Create an account";
        note.appendChild(a);
        note.appendChild(document.createTextNode(" to keep this deck."));
      } else {
        note.appendChild(document.createTextNode("Create an account to keep this deck."));
      }
      readyPanel.appendChild(note);
    }
    readyPanel.hidden = false;
  }
}
