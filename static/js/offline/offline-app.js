// offline-app.js: bootstrap for the /offline shell. Client-rendered
// on purpose: the shell is served from the SW cache with no network
// and no server-resolved user, so everything here reads IndexedDB
// (docs/OFFLINE.md section 3).
//
// The study-loop views (card, reveal, verdict, caught-up, add-a-card)
// live in static/js/study/components.js and the dashboard views
// (prelude, due strip, deck list) in static/js/dashboard/components.js;
// both are storage-agnostic, and this shell drives them against the
// LocalSource of each port over the offline stores. What stays here:
// the status line, the needs-attention list, the storage readout, and
// the reconnect flow (banner, outbox flush via sync.js, owner-conflict
// dialog).
//
// Plain DOM building, no framework, no innerHTML for data (card
// prompts, choices, and answers are user content; textContent only,
// with prompts going through the study components' escape-first
// markdown renderer).

import {getAll, metaGet, remove, withLock} from "./store.js";
import {
  flushOutbox,
  maybeConfirmOwnerConflict,
  refreshSnapshot,
  showToast,
  wipeLegacyGuestData,
} from "./sync.js";
import {el, prelude, sectionEyebrow} from "../study/dom.js";
import {
  authorView,
  caughtUpView,
  revealView,
  runPending,
  setActionErrorHandler,
  studyCardView,
  verdictView,
} from "../study/components.js";
import {LocalSource, nextDueInMinutes} from "../study/source.js";
import {deckListView, dueStripView, preludeView} from "../dashboard/components.js";
import {LocalSource as DeckLocalSource} from "../dashboard/local-source.js";

// The deploy's root path, derived from this module's own URL (same
// trick as sync.js: the module is served under <root>/static/js/...).
const ROOT_PATH = new URL(import.meta.url).pathname.replace(/\/static\/js\/.*$/, "");

setActionErrorHandler((e) => {
  console.warn("offline study action failed:", e);
  showToast("Could not save that. Try again.");
});

// ---- state -----------------------------------------------------------

const state = {
  owner: null,
  decks: [],
  cards: [],
  localCards: [],
  outbox: [],
  rejects: [],
  storage: null,
};

const source = new LocalSource(state);
// The dashboard port over the rows this shell already holds: one read
// of IndexedDB per reload, shared by the study loop and the overview.
const deckSource = new DeckLocalSource({read: () => state});

let root = null;
let viewName = "loading";

// navigator.storage.estimate() + persisted(), folded into one small
// readout for the overview's footer line. Null when the platform
// hides the API (older WebKit): the line is simply omitted.
async function readStorage() {
  try {
    if (!navigator.storage || !navigator.storage.estimate) return null;
    const [estimate, persisted] = await Promise.all([
      navigator.storage.estimate(),
      navigator.storage.persisted ? navigator.storage.persisted() : Promise.resolve(false),
    ]);
    return {usage: estimate.usage || 0, persisted: Boolean(persisted)};
  } catch (e) {
    return null;
  }
}

function humanBytes(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1).replace(/\.0$/, "") + " KB";
  if (n < 1024 * 1024 * 1024) return (n / (1024 * 1024)).toFixed(1).replace(/\.0$/, "") + " MB";
  return (n / (1024 * 1024 * 1024)).toFixed(2) + " GB";
}

async function reloadLocal() {
  const [owner, decks, cards, localCards, outbox, rejects, storage] = await Promise.all([
    metaGet("owner"),
    getAll("decks"),
    getAll("cards"),
    getAll("local_cards"),
    getAll("outbox_reviews"),
    getAll("rejects"),
    readStorage(),
  ]);
  state.owner = owner;
  state.decks = decks;
  state.cards = cards;
  state.localCards = localCards;
  state.outbox = outbox;
  state.rejects = rejects;
  state.storage = storage;
}

// The study queue draws from both snapshot cards and locally authored
// ones. A local card has client_id and no question_id; it studies as
// a short self-verdict card (no type field, so grader.grade returns
// null and the reveal flow takes over).
function allStudyCards() {
  return state.cards.concat(state.localCards);
}

function show(node, name) {
  root.replaceChildren(node);
  viewName = name;
  window.scrollTo(0, 0);
}

// ---- views -----------------------------------------------------------

// The no-owner render. Without a stamped owner the shell cannot say
// whose cards these are, so it opens no study loop -- but locally
// authored rows still flush on the next online load, and telling
// their owner the device holds nothing would be a lie.
function renderEmpty() {
  const pending = state.localCards.length;
  show(
    pending
      ? prelude(
          "Offline study",
          "Waiting to",
          "sync",
          pending + (pending === 1 ? " card is" : " cards are") +
            " saved on this device and not synced yet. Open prep while " +
            "online to finish saving them; your decks and due cards are " +
            "cached for offline study after that."
        )
      : prelude(
          "Offline study",
          "Nothing cached",
          "yet",
          "Open prep while online first. Your decks and due cards are " +
            "saved to this device automatically, and this screen works " +
            "without a connection after that."
        ),
    "empty"
  );
}

// The one line this surface says about ITSELF: that it IS the offline
// surface, whose snapshot this is, how old, and what has not reached
// the server. Everything else on the screen is the shared dashboard,
// rendered from the same views the signed-in page runs, so this line
// is the only thing telling the user which of the two they are on.
// The condition leads: the shell is a service-worker navigation
// fallback, so the user asked for the live page and got this one.
function statusLine(overview) {
  const bits = ["Offline."];
  bits.push("Studying as " + ((overview.user && overview.user.display_name) || "you") + ".");
  if (state.owner.snapshot_at) {
    const stamp = Date.parse(state.owner.snapshot_at);
    const label = Number.isFinite(stamp)
      ? new Date(stamp).toLocaleString()
      : state.owner.snapshot_at;
    bits.push("Snapshot from " + label + ".");
  }
  const unsynced = overview.unsynced || {reviews: 0, cards: 0};
  const waiting = [];
  if (unsynced.reviews) {
    waiting.push(unsynced.reviews + (unsynced.reviews === 1 ? " review" : " reviews"));
  }
  if (unsynced.cards) {
    waiting.push(unsynced.cards + (unsynced.cards === 1 ? " new card" : " new cards"));
  }
  if (waiting.length) bits.push(waiting.join(" and ") + " waiting to sync.");
  return bits.join(" ");
}

async function renderOverview() {
  const overview = await deckSource.overview();
  const frag = document.createDocumentFragment();

  frag.appendChild(preludeView(overview, {status: statusLine(overview)}));
  // This surface's study session spans every deck, which is the one
  // action the strip offers.
  frag.appendChild(dueStripView(overview, {onStudy: () => startStudy()}));
  frag.appendChild(
    deckListView(overview, {
      // No deck pages and no server routes offline, so no hrefs and no
      // row menus: the same rows, rendered from what this device can
      // actually answer.
      actions: [{glyph: "+", label: "add a card", onClick: () => renderAuthor()}],
      empty: {ctaLabel: "Add a card", ctaOnClick: () => renderAuthor()},
    })
  );

  // ---- needs attention (server-rejected items) ----------------------
  if (state.rejects.length) frag.appendChild(renderRejects());

  // ---- device footer lines ------------------------------------------
  if (state.storage) {
    // Quiet debugging readout (docs/OFFLINE.md section 3), not a nag:
    // how much the origin is using, and whether the platform granted
    // the persistence request sync.js makes after snapshot writes.
    frag.appendChild(
      el(
        "p",
        "muted offline-storage-note",
        "Offline storage: " + humanBytes(state.storage.usage) + " used" +
          (state.storage.persisted ? " · persistent" : "") + "."
      )
    );
  }
  // Safari-tab nudge (docs/OFFLINE.md section 3): a plain browser tab
  // is subject to Safari's 7-day script-storage cap; the installed
  // app is exempt and is the supported multi-day offline vehicle.
  if (window.matchMedia && !window.matchMedia("(display-mode: standalone)").matches) {
    frag.appendChild(
      el(
        "p",
        "muted offline-tab-nudge",
        "Tip: offline data is most durable in the installed app (Share, then Add to Home Screen)."
      )
    );
  }

  show(frag, "overview");
}

// ---- needs attention (rejects store) ---------------------------------

// Keep the disclosure open across the re-render a dismiss triggers.
let rejectsOpen = false;

// What a rejected row is ABOUT, one glanceable line: a rejected card
// shows its own prompt; a rejected review shows its card's prompt
// when the snapshot still has it, else the answer text it carried.
function rejectPreview(row) {
  if (row.kind === "card") {
    return row.prompt || (row.item && row.item.prompt) || "(no prompt recorded)";
  }
  if (row.question_id) {
    const card = state.cards.find((c) => c.question_id === row.question_id);
    if (card && card.prompt) return card.prompt;
  }
  return row.user_answer
    ? "Your answer: " + row.user_answer
    : "(no answer recorded)";
}

function truncate(text, max) {
  return text.length > max ? text.slice(0, max - 1) + "…" : text;
}

// The needs-attention list (docs/OFFLINE.md section 2): items the
// server permanently rejected at sync, surfaced instead of silently
// dropped. Count on the summary line, expandable rows underneath
// (kind, server error, preview), each with a per-row dismiss that
// deletes it from the rejects store.
function renderRejects() {
  const section = el("section", "offline-rejects");
  section.appendChild(sectionEyebrow("Needs attention"));
  const details = el("details", "offline-rejects-details");
  details.open = rejectsOpen;
  details.addEventListener("toggle", () => {
    rejectsOpen = details.open;
  });
  const n = state.rejects.length;
  const summary = el(
    "summary",
    "offline-rejects-summary",
    n + (n === 1 ? " item" : " items") + " couldn't sync"
  );
  // iOS 26 standalone swallows the synthesized click on <summary> for
  // ~5s after load (the details-toggle.js gotcha; that module is not
  // loaded in the shell). Own the toggle on pointerup and suppress
  // the late compatibility click.
  let toggledAt = 0;
  summary.addEventListener("pointerup", (e) => {
    e.preventDefault();
    toggledAt = Date.now();
    details.open = !details.open;
  });
  summary.addEventListener("click", (e) => {
    if (Date.now() - toggledAt < 500) e.preventDefault();
  });
  details.appendChild(summary);
  const list = el("ul", "offline-rejects-list");
  for (const row of state.rejects) {
    const item = el("li", "offline-reject");
    const head = el("div", "offline-reject-head");
    head.appendChild(el("span", "tag tag-type", row.kind === "card" ? "card" : "review"));
    head.appendChild(el("span", "offline-reject-error", row.error || "rejected"));
    item.appendChild(head);
    item.appendChild(el("p", "offline-reject-preview", truncate(rejectPreview(row), 140)));
    const dismiss = el("button", "offline-linkbtn offline-reject-dismiss", "Dismiss");
    dismiss.type = "button";
    dismiss.addEventListener("click", () => {
      runPending(dismiss, async () => {
        await withLock(() => remove("rejects", row.client_id));
        state.rejects = state.rejects.filter((r) => r.client_id !== row.client_id);
        renderOverview();
      });
    });
    item.appendChild(dismiss);
    list.appendChild(item);
  }
  details.appendChild(list);
  section.appendChild(details);
  return section;
}

// ---- the study loop --------------------------------------------------

async function startStudy() {
  const result = await source.next();
  if (result.card) renderStudyCard(result.card);
  else renderCaughtUp(result.caughtUp);
}

function renderStudyCard(card) {
  show(
    studyCardView(card, {
      onPause: () => renderOverview(),
      onAnswer: async (answer) => {
        const result = await source.submit(card, {answer});
        if (result.selfGrade) renderReveal(card, answer);
        else renderVerdict(card, result.verdict, answer, {minutes: result.nextDueMinutes, idk: false});
      },
      onIdk: async () => {
        const result = await source.submit(card, {idk: true});
        renderVerdict(card, "wrong", "", {minutes: result.nextDueMinutes, idk: true});
      },
    }),
    "study"
  );
}

function renderReveal(card, answer) {
  show(
    revealView(card, answer, {
      onPause: () => renderOverview(),
      onVerdict: async (verdict) => {
        const result = await source.submit(card, {verdict, answer});
        renderVerdict(card, verdict, answer, {minutes: result.nextDueMinutes, idk: false});
      },
    }),
    "reveal"
  );
}

function renderVerdict(card, verdict, userAnswer, opts) {
  show(
    verdictView(
      card,
      verdict,
      userAnswer,
      {
        minutes: opts.minutes,
        idk: opts.idk,
        // Marks divergence from the server's FSRS truth.
        scheduleNote: " · offline schedule",
      },
      {
        onNext: () => startStudy(),
        onPause: () => renderOverview(),
      }
    ),
    "verdict"
  );
}

function renderCaughtUp(summary) {
  const frag = document.createDocumentFragment();
  frag.appendChild(
    caughtUpView(summary || {nextDueMinutes: nextDueInMinutes(allStudyCards())}, {
      onAdd: () => renderAuthor(),
      onBack: () => renderOverview(),
    })
  );
  show(frag, "caughtup");
}

function renderAuthor() {
  show(
    authorView(
      {decks: state.decks},
      {
        onBack: () => renderOverview(),
        onSave: async (input) => {
          await source.author(input);
          showToast("Card added");
          renderOverview();
        },
      }
    ),
    "author"
  );
}

// ---- reconnect + sync ------------------------------------------------

let bannerNode = null;
let syncing = false;

function ensureBanner() {
  if (!bannerNode) {
    bannerNode = el("div", "offline-banner");
    bannerNode.setAttribute("role", "status");
    bannerNode.hidden = true;
    document.body.appendChild(bannerNode);
  }
  return bannerNode;
}

function showBanner(text, link) {
  const banner = ensureBanner();
  banner.replaceChildren(document.createTextNode(text));
  if (link) {
    banner.appendChild(document.createTextNode(" "));
    const a = el("a", null, link.label);
    a.href = link.href;
    banner.appendChild(a);
  }
  banner.hidden = false;
}

function hideBanner() {
  if (bannerNode) bannerNode.hidden = true;
}

// navigator.onLine alone lies on captive/one-bar networks; confirm
// with a lightweight probe against the un-auth-gated liveness route.
async function probeOnline() {
  if (!navigator.onLine) return false;
  try {
    const response = await fetch(ROOT_PATH + "/healthz", {cache: "no-store"});
    return response.ok;
  } catch (e) {
    return false;
  }
}

// When connectivity returns with reviews queued: banner, flush,
// forced snapshot refresh (which preserves overlays for anything
// still queued), toast the result. If the flush cannot run from the
// shell (dead session cookies, owner mismatch), hand off to the
// online app, which can mint fresh credentials via its reauth flow.
async function syncOnReconnect() {
  if (syncing) return;
  syncing = true;
  try {
    const [queued, localCards] = await Promise.all([
      getAll("outbox_reviews"),
      getAll("local_cards"),
    ]);
    if (!queued.length && !localCards.length) return;
    if (!(await probeOnline())) return;
    showBanner("Back online - syncing…");
    const result = await flushOutbox();
    const moved =
      (result.flushed || 0) +
      (result.rejected || 0) +
      (result.created || 0) +
      (result.rejectedCards || 0);
    if (result.disabled) {
      // The owner guard tripped: the signed-in session does not match
      // this device's snapshot owner. Surface the explicit
      // confirm-then-wipe dialog (never silent, never automatic);
      // after a wipe the stores hold the new account's snapshot, so
      // re-render from scratch. If the user already chose "keep" for
      // this account the prompt declines to re-open and the plain
      // handoff banner below takes over.
      const prompted = await maybeConfirmOwnerConflict({
        onWiped: async () => {
          await reloadLocal();
          // A reseed that failed mid-blip leaves no owner; the empty
          // state is the honest render then (same rule as boot).
          if (state.owner) renderOverview();
          else renderEmpty();
        },
      });
      if (prompted) {
        hideBanner();
        return;
      }
    }
    if (result.disabled || result.status || result.partial || moved === 0) {
      // partial = a transient failure mid-flush left chunks queued;
      // a success toast here would read as "all synced" while it
      // is not. Keep the handoff banner instead.
      showBanner("Back online.", {href: ROOT_PATH + "/", label: "Open prep to finish syncing."});
      return;
    }
    await refreshSnapshot({force: true});
    await reloadLocal();
    hideBanner();
    const bits = [];
    if (result.created) {
      bits.push(result.created === 1 ? "1 card added" : result.created + " cards added");
    }
    if (result.flushed) {
      bits.push(result.flushed === 1 ? "1 review synced" : result.flushed + " reviews synced");
    }
    const rejectedTotal = (result.rejected || 0) + (result.rejectedCards || 0);
    if (rejectedTotal) bits.push(rejectedTotal + " rejected");
    showToast(bits.join(", "));
    if (viewName === "overview") renderOverview();
    else if (viewName === "caughtup") renderCaughtUp();
  } catch (e) {
    console.warn("offline reconnect sync failed:", e);
    hideBanner();
  } finally {
    syncing = false;
  }
}

// ---- boot ------------------------------------------------------------

async function boot() {
  root = document.getElementById("offline-root") || document.body;
  try {
    // Ahead of every read below: this device may still hold rows a
    // retired client-side flow wrote, and they must not reach the
    // study queue or the outbox.
    await wipeLegacyGuestData();
    await reloadLocal();
  } catch (e) {
    // IndexedDB unavailable (private-mode quirks, storage wiped mid
    // read). Degrade to the honest empty state rather than a blank page.
    console.warn("offline app failed to read local data:", e);
    renderEmpty();
    return;
  }
  if (!state.owner) {
    renderEmpty();
    return;
  }
  await renderOverview();
  window.addEventListener("online", () => {
    syncOnReconnect();
  });
  window.addEventListener("offline", hideBanner);
  // The page may have been opened online with a queued outbox
  // (preflight, or study-then-reconnect without a reload).
  syncOnReconnect();
}

boot();
