// local-host.js: the landing page's dashboard, over this device's own
// snapshot. Reached only when the server could not identify the
// visitor (signed out of the identity provider, or the anonymous
// cookie is gone) while IndexedDB still holds their cards: a device
// with cards is shown its decks, never the splash.
//
// Same views as the signed-in page (dashboard/components.js) over the
// same DeckSource LocalSource the offline shell uses. What this
// surface can offer is narrower, and says so by DATA: no deck pages
// and no row menus without a session, and the one study action goes
// to the offline shell, which is the study loop that runs from the
// snapshot.
//
// Two callers, both landing-only: the pre-paint script in
// templates/landing.html when the flag says a snapshot exists, and
// app.js's probe when a snapshot predates the flag.

import {deckListView, dueStripView, preludeView} from "./components.js";
import {LocalSource} from "./local-source.js";
import {clearSnapshotFlag, markSnapshotHeld} from "../offline/store.js";

const ROOT_PATH = new URL(import.meta.url).pathname.replace(/\/static\/js\/.*$/, "");

// The study loop that runs from this snapshot, and the only route off
// this surface.
const OFFLINE_HREF = ROOT_PATH + "/offline";

// Stamped on <html> before paint; the stylesheet keys the splash and
// this region off it.
const FLAG_ATTR = "data-local-decks";

// The one line this surface says about ITSELF: why these decks are
// here and not the signed-in page, and what that costs on a shared
// browser. Everything else on the screen is the shared dashboard.
//
// This is the honest moment for it: the page is rendering one
// account's deck names to a visitor the server could not identify.
// So the line carries the exit as well, small and inline, because the
// owner of a personal phone reads it on every visit.
const STATUS =
  "Signed out. These cards are saved on this browser, so anyone who uses " +
  "it can open them.";

const STATUS_ACTION = "Remove them";

// Cards this browser holds that belong to no deck it holds (authored
// offline into a deck the snapshot never had). The deck list has no
// row for them, so the empty state carries the one action that does.
//
// "Browser" everywhere on this surface, including the status line
// above: it is what the storage is actually scoped to, and an
// installed PWA on iOS holds a different one from Safari on the same
// device.
const EMPTY_LOCAL = {
  headStart: "Cards saved ",
  headEm: "on this browser",
  sub:
    "These cards are not in a deck this browser holds a copy of. Study " +
    "them here, or sign in to sync them back.",
  ctaLabel: "Study on this browser",
};

let started = null;

// One mount per page load whichever caller gets there first.
export function mount() {
  if (!started) started = run();
  return started;
}

function whenParsed() {
  if (document.readyState !== "loading") return Promise.resolve();
  return new Promise((resolve) => {
    document.addEventListener("DOMContentLoaded", () => resolve(), {once: true});
  });
}

// A flag with nothing behind it (site data cleared unevenly, a wipe
// that stopped midway) must not leave the visitor on an empty
// dashboard: drop the flag and let the splash render.
function showSplash() {
  clearSnapshotFlag();
  document.documentElement.removeAttribute(FLAG_ATTR);
}

// The removal offered by the status line. The wipe module is imported
// on the tap, not with the host: it pulls the sync transport in, and
// this surface renders for a visitor who is not going to use it.
async function removeFromThisBrowser() {
  try {
    const wipe = await import("../offline/wipe.js");
    if (!(await wipe.confirmRemoveFromDevice())) return;
  } catch (e) {
    console.warn("removal from this browser failed:", e);
    return;
  }
  // The device now holds nothing, so the page this visitor should be
  // on is the splash the server renders.
  showSplash();
  window.location.reload();
}

function render(region, overview) {
  const frag = document.createDocumentFragment();
  // The device holds cards on every path that reaches here, so the
  // new-user welcome (the default when there are no deck rows) would
  // greet it with an invitation this surface cannot take up.
  frag.appendChild(
    preludeView(overview, {
      isNewUser: false,
      status: STATUS,
      statusAction: {label: STATUS_ACTION, onClick: removeFromThisBrowser},
    })
  );
  frag.appendChild(
    dueStripView(overview, {
      onStudy: () => window.location.assign(OFFLINE_HREF),
    })
  );
  frag.appendChild(
    deckListView(overview, {
      // The surface's one action, on every render: the strip above
      // offers study only when something is due, and the empty state's
      // CTA only when there are no deck rows.
      actions: [{icon: "arrow-up-right", label: "Study on this browser", href: OFFLINE_HREF}],
      empty: {...EMPTY_LOCAL, ctaHref: OFFLINE_HREF},
    })
  );
  region.replaceChildren(frag);
}

async function run() {
  try {
    await whenParsed();
    const region = document.querySelector("[data-landing-decks]");
    if (!region) return;
    const overview = await new LocalSource().overview();
    if (!overview.decks.length && !overview.total) {
      showSplash();
      return;
    }
    // A snapshot older than the flag writes it here, so the next load
    // of this page is decided before paint.
    markSnapshotHeld();
    document.documentElement.setAttribute(FLAG_ATTR, "");
    render(region, overview);
  } catch (e) {
    // A storage quirk must not strand the visitor on a hidden splash
    // above an empty region.
    console.warn("local dashboard did not mount:", e);
    showSplash();
  }
}
