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

// ---- Landing "study offline" link (hook-gated) -----------------------
// The landing template ships the link hidden; the module reveals it
// only when this device already holds an offline snapshot. Lazy
// import gated on the hook so every other page (and every visitor
// without the hook) pays one querySelector and nothing else.
const offlineLinkHook = document.querySelector("[data-offline-link]");
if (offlineLinkHook) {
  import("@/modules/offline-link.js")
    .then((m) => m.init(offlineLinkHook))
    .catch((e) => console.warn("offline link module unavailable:", e));
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
