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

initDetailsToggle();
attachDialogs();
attachSubmitPending();
attachSheets();

// Workflow polling pages (transform, plan, grading, trivia gen) now
// drive their polling via htmx's `hx-trigger="every Ns"` on a fragment
// route — see partials/*_progress.html. The old `[data-poll-url]` hook
// + poller.js module are gone (deleted in htmx-7).
