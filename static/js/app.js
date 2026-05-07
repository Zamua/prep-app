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
import {attachDeclarative as attachSubmitAjax} from "@/modules/submit-ajax.js";

initDetailsToggle();
attachDialogs();
attachSubmitPending();
attachSubmitAjax();

// Lazy-load the poller only when a page asks for it. Keeps the cold
// boot light for non-polling pages (most of them).
if (document.querySelector("[data-poll-url]")) {
  import("@/modules/poller.js").then(({attachDeclarative}) => attachDeclarative());
}
