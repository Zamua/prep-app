// qcard-suspend — inline suspend / unsuspend toggle for qcards on
// the deck page. Avoids the full-page POST→303→GET cycle that would
// lose scroll position on long deck pages. Optimistic class toggle
// for instant feedback; falls back to a real form submit if the fetch
// fails.
//
// Listens on document so cards rendered later (e.g. after htmx
// inserts) still get the behavior without re-binding.
//
// Usage:
//   import {init} from "@/modules/qcard-suspend.js";
//   init();

const SUSPEND_FORM_CLASSES = new Set([
  "qcard-action-form--suspend",
  "qcard-action-form--unsuspend",
]);

export function init() {
  document.addEventListener("submit", async (e) => {
    const form = e.target;
    const matches = [...form.classList].some((c) => SUSPEND_FORM_CLASSES.has(c));
    if (!matches) return;
    const qcard = form.closest(".qcard");
    if (!qcard) return;
    e.preventDefault();

    const willSuspend = form.classList.contains("qcard-action-form--suspend");
    qcard.classList.toggle("is-suspended", willSuspend);

    try {
      const r = await fetch(form.action, {
        method: "POST",
        headers: {accept: "text/html"},
        // Tell the server (or any caching middleware) we're an in-page
        // request, not a navigation. Mostly informational.
        redirect: "manual",
      });
      // Any 2xx or 3xx (the redirect we never follow) means the write
      // landed. opaqueredirect lands here too because of redirect:manual.
      if (!r.ok && r.type !== "opaqueredirect" && r.status !== 0) {
        throw new Error("non-success: " + r.status);
      }
      form.querySelector("button")?.blur();
    } catch (err) {
      // Revert optimistic toggle, fall back to real submit so the user
      // gets the canonical full-page error path.
      qcard.classList.toggle("is-suspended", !willSuspend);
      form.submit();
    }
  });
}
