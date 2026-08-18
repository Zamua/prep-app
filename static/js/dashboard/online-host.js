// online-host.js: the signed-in dashboard. Wires the embedded
// overview (then ServerSource) to the shared dashboard views, owns
// the mounts, the deck URLs, and the server-composed row menus. The
// offline shell (offline/offline-app.js) is the same wiring over
// LocalSource; the views themselves are shared and know neither host.

import {deckListView, preludeView} from "./components.js";
import {ServerSource, readEmbeddedOverview} from "./source.js";

const ROOT_PATH = new URL(import.meta.url).pathname.replace(/\/static\/js\/.*$/, "");

const state = {
  prelude: null,
  decks: null,
  menus: null,
  source: null,
  overview: null,
  isNewUser: false,
  hasSessions: false,
  agentAvailable: false,
};

// ---- server-composed pieces --------------------------------------------

// The row menu for one deck, cloned out of the shell's inert
// <template>. Every row of it is a server route, so the markup comes
// from the server; a deck with no menu (none rendered for it) gets no
// menu rather than a client-built stand-in.
function findMenu(deck) {
  if (!state.menus) return null;
  return state.menus.querySelector(`[data-deck-menu="${CSS.escape(String(deck.id))}"]`);
}

function deckMenu(deck) {
  const node = findMenu(deck);
  return node ? node.cloneNode(true) : null;
}

function deckHref(deck) {
  return deck.slug ? ROOT_PATH + "/deck/" + encodeURIComponent(deck.slug) : null;
}

function actions(overview) {
  const rows = [{glyph: "+", label: "new deck", href: ROOT_PATH + "/decks/new"}];
  if (state.agentAvailable && overview.decks.length) {
    rows.push({icon: "sparkle", label: "edit with AI", href: ROOT_PATH + "/reorganize"});
  }
  return rows;
}

// ---- render ------------------------------------------------------------

function render(overview) {
  state.overview = overview;
  state.prelude.replaceChildren(
    preludeView(overview, {
      isNewUser: state.isNewUser,
      agentAvailable: state.agentAvailable,
    })
  );
  state.decks.replaceChildren(
    deckListView(overview, {
      deckHref,
      deckMenu,
      actions: actions(overview),
      hasSessions: state.hasSessions,
      isNewUser: state.isNewUser,
      empty: {ctaHref: ROOT_PATH + "/decks/new"},
    })
  );
  // The menus carry htmx attributes of their own (the trivia interval
  // editor). htmx only wires what it parsed, so freshly mounted nodes
  // have to be handed to it.
  if (window.htmx) window.htmx.process(state.decks);
}

function paint(overview) {
  if (!document.startViewTransition) {
    render(overview);
    return;
  }
  const transition = document.startViewTransition(() => render(overview));
  // A skipped transition (reduced motion, hidden tab, headless) rejects
  // its promises; the render itself still ran, so the skip is not an
  // error and must not surface as an unhandled rejection.
  for (const q of [transition.ready, transition.finished]) q.catch(() => {});
}

// ---- pin toggle --------------------------------------------------------

// The menu's Pin row POSTs a server route, so the toggle is the
// server's to apply; the host only re-reads afterwards. Both reads
// matter: the counts move a card between sections, and the menu's own
// row now offers the opposite action.
async function refresh() {
  const [overview, menus] = await Promise.all([state.source.overview(), fetchMenus()]);
  if (menus) state.menus = menus;
  // The two are independent reads: a deck created between them is in
  // the overview and in no menu, and its row would render without an
  // overflow trigger until the next action. Re-read once when that
  // shows up rather than painting the gap.
  if (overview.decks.some((deck) => !findMenu(deck))) {
    state.menus = await fetchMenus().catch(() => state.menus);
  }
  paint(overview);
}

async function fetchMenus() {
  const res = await fetch(ROOT_PATH + "/api/dashboard/deck-menus", {
    credentials: "same-origin",
    cache: "no-store",
  });
  if (!res.ok) throw new Error("deck menus request failed (" + res.status + ")");
  const holder = document.createElement("template");
  holder.innerHTML = await res.text();
  return holder.content;
}

async function onPinSubmit(form) {
  const res = await fetch(form.action, {
    method: "POST",
    body: new FormData(form),
    credentials: "same-origin",
    redirect: "manual",
  });
  if (!res.ok && res.type !== "opaqueredirect" && res.status !== 0) {
    throw new Error("pin failed (" + res.status + ")");
  }
  await refresh();
}

function attachPinHandler() {
  state.decks.addEventListener("submit", (e) => {
    const form = e.target;
    if (!form.classList || !form.classList.contains("deck-pin-menu-form")) return;
    e.preventDefault();
    form.closest("details.row-overflow-menu")?.removeAttribute("open");
    onPinSubmit(form).catch(() => {
      // Hand the canonical POST + redirect path the failure, the same
      // fallback the session-discard flow uses.
      form.submit();
    });
  });
}

// ---- boot --------------------------------------------------------------

export async function init() {
  state.prelude = document.querySelector("[data-dashboard-prelude]");
  state.decks = document.querySelector("[data-dashboard-decks]");
  if (!state.prelude || !state.decks) return;
  state.isNewUser = state.decks.dataset.newUser === "1";
  state.hasSessions = state.decks.dataset.hasSessions === "1";
  state.agentAvailable = state.decks.dataset.agentAvailable === "1";
  const template = document.querySelector("template[data-deck-menus]");
  state.menus = template ? template.content : null;
  state.source = new ServerSource();
  attachPinHandler();

  // The shell embeds the overview this same request already read, so
  // the first paint costs no round trip. Only a shell without one
  // (or with an unreadable one) opens the page on a fetch.
  const embedded = readEmbeddedOverview();
  if (embedded) render(embedded);
  else await refresh();
}

// A throw here leaves the shell's fallback note mounted, which is what
// tells the user the list never arrived; nothing else would.
init().catch((e) => console.warn("dashboard did not mount:", e));
