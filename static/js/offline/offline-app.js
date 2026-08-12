// offline-app.js: bootstrap for the /offline shell. Client-rendered
// on purpose: the shell is served from the SW cache with no network
// and no server-resolved user, so everything here reads IndexedDB
// (docs/OFFLINE.md section 3).
//
// The study-loop views (card, reveal, verdict, caught-up, add-a-card)
// live in static/js/study/components.js and are storage-agnostic;
// this shell drives them against a LocalSource (static/js/study/
// source.js) over the offline stores. What stays here: the overview
// (owner line, per-deck counts, due list), the needs-attention list,
// the guest-mode nudges, the storage readout, and the reconnect flow
// (banner, outbox flush via sync.js, owner-conflict dialog).
//
// Plain DOM building, no framework, no innerHTML for data (card
// prompts, choices, and answers are user content; textContent only,
// with prompts going through the study components' escape-first
// markdown renderer).

import {getAll, metaGet, put, remove, withLock} from "./store.js";
import {flushOutbox, maybeConfirmOwnerConflict, refreshSnapshot, showToast} from "./sync.js";
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
import {LocalSource, compareStudyOrder, isDueNow, nextDueInMinutes} from "../study/source.js";

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
  guest: null,
  guestNudge: null,
  decks: [],
  cards: [],
  localCards: [],
  outboxCount: 0,
  rejects: [],
  storage: null,
};

const source = new LocalSource(state);

let root = null;
let viewName = "loading";

// Where the guest-mode account nudges send the visitor; supplied by
// the /offline route as a data attribute on the root element (empty
// on providers without a hosted sign-in flow). Read at boot.
let signInUrl = "";

// Guest mode is a client-side derivation: no snapshot owner, but
// guest data present (meta.guest, or owner-absent local cards). Same
// definition as sync.js guestAdoptionPending.
function isGuest() {
  return !state.owner && Boolean(state.guest || state.localCards.length);
}

function guestDeckName() {
  if (state.guest && state.guest.display_name) return state.guest.display_name;
  const withName = state.localCards.find((c) => c.deck_name);
  return withName ? withName.deck_name : "Your cards";
}

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
  const [owner, guest, guestNudge, decks, cards, localCards, outbox, rejects, storage] =
    await Promise.all([
      metaGet("owner"),
      metaGet("guest"),
      metaGet("guest_nudge"),
      getAll("decks"),
      getAll("cards"),
      getAll("local_cards"),
      getAll("outbox_reviews"),
      getAll("rejects"),
      readStorage(),
    ]);
  state.owner = owner;
  state.guest = guest;
  state.guestNudge = guestNudge;
  state.decks = decks;
  state.cards = cards;
  state.localCards = localCards;
  state.outboxCount = outbox.length;
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

function renderEmpty() {
  show(
    prelude(
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

function renderOverview() {
  const now = Date.now();
  const dueCards = allStudyCards()
    .filter((card) => isDueNow(card, now))
    .sort(compareStudyOrder);

  const frag = document.createDocumentFragment();

  // ---- prelude + owner line -----------------------------------------
  const dueLede = dueCards.length
    ? dueCards.length + (dueCards.length === 1 ? " card is" : " cards are") +
      " due right now."
    : "Nothing is due right now.";
  if (state.owner) {
    const who = state.owner.display_name || "you";
    frag.appendChild(prelude("Offline study", "Your cards,", "offline", "Studying as " + who + ". " + dueLede));
  } else {
    // Guest mode: no owner to study as; the deck's own name is the
    // headline. Owner-null guard, not just copy: the boot gate admits
    // owner-absent guest data.
    const section = el("section", "prelude");
    section.appendChild(el("p", "eyebrow", "Your deck"));
    section.appendChild(el("h1", "display", guestDeckName()));
    section.appendChild(el("p", "lede", dueLede));
    frag.appendChild(section);
  }

  // ---- study CTA + authoring entry ----------------------------------
  const actions = el("div", "study-actions offline-study-cta");
  if (dueCards.length) {
    const studyBtn = el("button", "btn btn-primary", "Study");
    studyBtn.type = "button";
    studyBtn.addEventListener("click", () => startStudy());
    actions.appendChild(studyBtn);
  }
  const addBtn = el("button", "btn btn-quiet", "Add a card");
  addBtn.type = "button";
  addBtn.addEventListener("click", () => renderAuthor());
  actions.appendChild(addBtn);
  frag.appendChild(actions);

  // ---- per-deck counts ----------------------------------------------
  const deckSection = el("section", "offline-decks");
  deckSection.appendChild(sectionEyebrow("Decks"));
  if (isGuest()) {
    // The snapshot decks store is empty for guests: the one guest
    // deck renders from meta.guest with live counts.
    const total = state.localCards.length;
    const due = state.localCards.filter((card) => isDueNow(card, now)).length;
    const list = el("ul", "offline-deck-list");
    const item = el("li", "offline-deck");
    item.appendChild(el("span", "offline-deck-name", guestDeckName()));
    item.appendChild(
      el(
        "span",
        "offline-deck-counts muted",
        due + " due · " + total + (total === 1 ? " card" : " cards")
      )
    );
    list.appendChild(item);
    deckSection.appendChild(list);
  } else if (state.decks.length === 0) {
    deckSection.appendChild(el("p", "muted", "No decks in the snapshot."));
  } else {
    const byDeck = new Map();
    for (const card of state.cards) {
      const entry = byDeck.get(card.deck_id) || {total: 0, due: 0};
      entry.total += 1;
      if (isDueNow(card, now)) entry.due += 1;
      byDeck.set(card.deck_id, entry);
    }
    const list = el("ul", "offline-deck-list");
    for (const deck of state.decks) {
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

  // ---- due-now list (glanceable prompts) ----------------------------
  const dueSection = el("section", "offline-due");
  dueSection.appendChild(sectionEyebrow("Due now"));
  if (dueCards.length === 0) {
    // Guests have no account to sync against; the "come back online"
    // clause only makes sense once an owner exists.
    dueSection.appendChild(
      el(
        "p",
        "muted",
        state.owner
          ? "Nothing due. Check back later, or come back online to sync."
          : "Nothing due right now. Check back later."
      )
    );
  } else {
    const list = el("ul", "offline-due-list");
    for (const card of dueCards) {
      list.appendChild(el("li", "offline-due-card", card.prompt || ""));
    }
    dueSection.appendChild(list);
  }
  frag.appendChild(dueSection);

  // ---- needs attention (server-rejected items) ----------------------
  if (state.rejects.length) frag.appendChild(renderRejects());

  // ---- footer lines -------------------------------------------------
  // The waiting-to-sync notes describe the flush into an ACCOUNT.
  // Guests have nothing to sync against; the disclosure line below
  // carries their storage story instead.
  if (state.owner && state.outboxCount) {
    frag.appendChild(
      el(
        "p",
        "muted offline-outbox-note",
        state.outboxCount +
          (state.outboxCount === 1 ? " review" : " reviews") +
          " waiting to sync."
      )
    );
  }
  if (state.owner && state.localCards.length) {
    frag.appendChild(
      el(
        "p",
        "muted offline-newcards-note",
        state.localCards.length +
          (state.localCards.length === 1 ? " new card" : " new cards") +
          " waiting to sync."
      )
    );
  }
  if (state.owner && state.owner.snapshot_at) {
    const stamp = Date.parse(state.owner.snapshot_at);
    const label = Number.isFinite(stamp)
      ? new Date(stamp).toLocaleString()
      : state.owner.snapshot_at;
    frag.appendChild(el("p", "muted offline-snapshot-stamp", "Snapshot from " + label + "."));
  } else if (isGuest()) {
    // The persistent account nudge replaces the snapshot-stamp line:
    // there is no snapshot, only this device.
    frag.appendChild(guestDisclosureLine());
  }
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

// The persistent guest account nudge (footer line). Quiet, always
// present in guest mode, never animated, never blocking.
function guestDisclosureLine() {
  const line = el("p", "muted offline-guest-note");
  line.appendChild(document.createTextNode("This deck is stored only on this device. "));
  if (signInUrl) {
    const a = el("a", null, "Create an account");
    a.href = signInUrl;
    line.appendChild(a);
  } else {
    line.appendChild(document.createTextNode("Create an account"));
  }
  line.appendChild(document.createTextNode(" to keep it across your devices."));
  return line;
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
        // The "offline schedule" qualifier marks divergence from the
        // server's FSRS truth; for a guest the ladder IS the schedule.
        scheduleNote: isGuest() ? null : " · offline schedule",
      },
      {
        onNext: () => startStudy(),
        onPause: () => renderOverview(),
      }
    ),
    "verdict"
  );
}

// Post-session account nudge: shown at most once per page load, only
// in guest mode, only until dismissed, never as a modal, never over a
// card. "Not now" stamps meta.guest.nudge_dismissed_at, or the
// meta.guest_nudge record when no guest record exists (the footer
// disclosure line stays).
let nudgeShownThisLoad = false;

function guestNudgeBanner() {
  const banner = el("section", "offline-guest-nudge");
  banner.appendChild(
    el(
      "p",
      "offline-guest-nudge-copy",
      "Nice work. This deck lives only on this device so far. " +
        "Create a free account to keep it and study anywhere."
    )
  );
  const actions = el("div", "offline-guest-nudge-actions");
  const cta = el("a", "btn btn-primary", "Create a free account");
  cta.href = signInUrl;
  actions.appendChild(cta);
  const dismiss = el("button", "btn btn-quiet", "Not now");
  dismiss.type = "button";
  dismiss.addEventListener("click", () => {
    runPending(dismiss, async () => {
      if (state.guest) {
        const stamped = {...state.guest, nudge_dismissed_at: new Date().toISOString()};
        await withLock(() => put("meta", stamped, "guest"));
        state.guest = stamped;
      } else {
        // Authored-only guest (owner-absent local cards, no
        // meta.guest record): the dismissal needs its own meta key
        // to survive a reload.
        const stamped = {dismissed_at: new Date().toISOString()};
        await withLock(() => put("meta", stamped, "guest_nudge"));
        state.guestNudge = stamped;
      }
      banner.remove();
    });
  });
  actions.appendChild(dismiss);
  banner.appendChild(actions);
  return banner;
}

function renderCaughtUp(summary) {
  const frag = document.createDocumentFragment();
  if (
    isGuest() &&
    signInUrl &&
    !nudgeShownThisLoad &&
    !(state.guest && state.guest.nudge_dismissed_at) &&
    !(state.guestNudge && state.guestNudge.dismissed_at)
  ) {
    nudgeShownThisLoad = true;
    frag.appendChild(guestNudgeBanner());
  }
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
    // Guest mode (no snapshot owner): there is no account to sync
    // with until the online app's adoption confirm decides, so
    // reconnect is a no-op. No flush, no banner nag.
    if (!(await metaGet("owner"))) return;
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
  signInUrl = (root.dataset && root.dataset.signInUrl) || "";
  try {
    await reloadLocal();
  } catch (e) {
    // IndexedDB unavailable (private-mode quirks, storage wiped mid
    // read). Degrade to the honest empty state rather than a blank page.
    console.warn("offline app failed to read local data:", e);
    renderEmpty();
    return;
  }
  // Empty only when there is neither an owner nor guest data;
  // owner-absent with guest data boots into the guest-mode overview.
  if (!state.owner && !isGuest()) {
    renderEmpty();
    return;
  }
  if (isGuest()) {
    // Guest mode reads as the app, not the fallback: plain "Prep",
    // no "offline" suffix.
    const brand = document.querySelector(".masthead .brand-word");
    if (brand) brand.textContent = "Prep";
  }
  renderOverview();
  window.addEventListener("online", () => {
    syncOnReconnect();
  });
  window.addEventListener("offline", hideBanner);
  // The page may have been opened online with a queued outbox
  // (preflight, or study-then-reconnect without a reload).
  syncOnReconnect();
}

boot();
