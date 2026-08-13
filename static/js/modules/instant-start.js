// instant-start.js: the landing hero's anonymous generation loop
// (docs/ANONYMOUS-ACCOUNTS.md section 3). Four form states: idle,
// generating, ready, error. The server stores the deck and answers
// with the URL to land on, so a successful generation ends in a
// navigation and nothing here writes card data.
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
  const continueStrip = node.querySelector("[data-instant-continue]");
  if (!form || !textarea || !button || !statusLine || !errorLine) return;
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
        if (!response || !response.ok || !body || typeof body.redirect !== "string") {
          showError(response, body);
          return; // input preserved
        }
        // Straight into the deck the server stored. A summary screen
        // here would be a second deck view competing with the real one.
        window.location.assign(body.redirect);
        return;
      } finally {
        button.classList.remove("is-loading");
        statusLine.hidden = true;
      }
    } finally {
      inFlight = false;
    }
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
    if (kind === "deck_limit") {
      return (body && typeof body.message === "string" && body.message) ||
        "You've reached the limit for a guest account. Create a free account to keep going.";
    }
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
}
