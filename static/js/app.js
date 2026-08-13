// Bootstrap. Loaded once from base.html as <script type="module">.
// Initializes the always-on behaviors (details toggle, dialogs,
// submit-pending) and lazy-imports per-feature modules when their
// data-* hooks are present on the page.
//
// Convention: behaviors that ALWAYS need to run app-wide go in the
// init block below. Behaviors driven by data-* attributes go through
// attachDeclarative() so adding the attribute to a template wires
// the behavior — no per-page boilerplate.

import {init as initDetailsToggle} from "@/modules/details-toggle.js";
import {attachDeclarative as attachDialogs} from "@/modules/dialog.js";
import {attachDeclarative as attachSubmitPending} from "@/modules/submit-pending.js";
import {attachDeclarative as attachSheets} from "@/modules/sheet.js";
import {init as initPwaInstall} from "@/modules/pwa-install.js";
import {init as initRowOverflow} from "@/modules/row-overflow.js";
import {attachDeclarative as attachByokToggles} from "@/modules/byok-key-toggle.js";
import {attachDeclarative as attachCopyButtons} from "@/modules/copy-button.js";

initDetailsToggle();
attachDialogs();
attachSubmitPending();
attachSheets();
initPwaInstall();
initRowOverflow();
attachByokToggles();
attachCopyButtons();

// ---- Service worker (always-on) --------------------------------------
// Registered app-wide so an installed PWA has a SW even if the user
// never touched notifications (the offline shell depends on it).
// Registration is idempotent: calling register() with the same URL +
// scope on every page load is a no-op after the first.
//
// The deploy's root path is derived from this module's own URL:
// base.html loads app.js at <root>/static/js/..., so stripping the
// /static/js/ tail leaves the root prefix ("" on a bare-host deploy).
// Same prefix the manifest route uses for scope/start_url
// (prep/web/pwa.py), just resolved client-side.
const ROOT_PATH = new URL(import.meta.url).pathname.replace(/\/static\/js\/.*$/, "");

if ("serviceWorker" in navigator) {
  navigator.serviceWorker
    .register(ROOT_PATH + "/sw.js", {scope: ROOT_PATH + "/"})
    .catch((e) => console.warn("SW register failed:", e));
}

// ---- Landing: the decks this device holds (hook-gated) ---------------
// The landing page's pre-paint script mounts the local dashboard when
// the snapshot flag says this device holds cards. A snapshot written
// before that flag existed has none, so ask IndexedDB once, after
// paint: the host stamps the flag as it mounts, and the next load of
// this page is decided before paint like everyone else's. Those
// devices see the splash for that one load; no other visitor does.
//
// store.js is already on this page's module graph (sync.js imports
// it below), so the probe costs a read, not a download.
const landingDecks = document.querySelector("[data-landing-decks]");
if (landingDecks && !document.documentElement.hasAttribute("data-local-decks")) {
  import("@/offline/store.js")
    .then((store) => store.metaGet("owner"))
    .then((owner) => {
      if (!owner || !owner.user_id) return null;
      // The host's chain is four import levels deep and this path has
      // the splash on screen while it loads. The landing's head script
      // knows the file list; inject it so the levels fetch as one.
      if (window.__prepPreloadLocalDecks) window.__prepPreloadLocalDecks();
      return import("@/dashboard/local-host.js");
    })
    .then((host) => host && host.mount())
    // The splash is the safe answer when storage cannot be read.
    .catch((e) => console.warn("local deck probe failed:", e));
}

// ---- Forget this device (hook-gated) ---------------------------------
// The account and its decks survive on the server; the browser the
// user asked to forget must not keep a copy it can still render. The
// snapshot is client-side, so the wipe happens here, before the POST
// that drops the cookie. A cancelled confirm has already prevented
// the event, and the resubmit skips this listener's own guard.
const forgetForm = document.querySelector("[data-forget-device]");
if (forgetForm) {
  let wiping = false;
  forgetForm.addEventListener("submit", (e) => {
    if (wiping || e.defaultPrevented) return;
    e.preventDefault();
    wiping = true;
    const wiped = import("@/offline/store.js")
      .then((store) => store.wipeAll())
      .catch((err) => console.warn("offline wipe before forget failed:", err));
    // A blocked IndexedDB open settles neither handler, so awaiting the
    // wipe alone can leave the POST unsent and the button dead. The
    // cookie has to go regardless; a snapshot the wipe could not reach
    // is caught by the owner guard on the next sign-in.
    const deadline = new Promise((resolve) => setTimeout(resolve, 2000));
    Promise.race([wiped, deadline]).then(() => forgetForm.submit());
  });
}

// ---- Instant-start landing hero (hook-gated) --------------------------
// The anonymous generation form. Hook-gated like the block above so
// only the instant landing loads the module.
const instantStartHook = document.querySelector("[data-instant-start]");
if (instantStartHook) {
  import("@/modules/instant-start.js")
    .then((m) => m.init(instantStartHook))
    .catch((e) => console.warn("instant start module unavailable:", e));
}

// ---- Offline snapshot refresh (fire-and-forget) ----------------------
// Keeps the IndexedDB snapshot warm on online pages so an offline cold
// launch has decks + cards to show. Lazy dynamic import so a failure
// here can never take the page's other behaviors down with it.
import("@/offline/sync.js")
  .then((m) => m.init())
  .catch((e) => console.warn("offline sync unavailable:", e));

// Workflow polling pages (transform, plan, grading, trivia gen) now
// drive their polling via htmx's `hx-trigger="every Ns"` on a fragment
// route — see partials/*_progress.html. The old `[data-poll-url]` hook
// + poller.js module are gone (deleted in htmx-7).
