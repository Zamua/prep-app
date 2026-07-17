// Disable + (optionally) label-swap a submit button on form submit so a
// double-tap doesn't fire two POSTs while the server kicks off a
// workflow + 303s to a polling page. Re-enables on `pageshow` if the
// user back-navigates to the form (e.g. after a server-side validation
// 400).
//
// Two flavors:
//
//   <form data-submit-pending>
//     <button type="submit" data-pending-label="Generating…">Submit</button>
//   </form>
//   → disables, swaps text to "Generating…", adds .is-loading
//
//   <form data-submit-pending>
//     <button type="submit"><svg/> <span>Pin</span></button>
//   </form>
//   → disables, adds .is-loading; text is left alone (so icon-bearing
//     buttons don't have their SVG clobbered by textContent replace).
//     CSS uses .is-loading to show a spinner + hide the icon.
//
// Multi-submit forms (e.g. the trivia answer form's Submit + "I don't
// know"): the pending state lands on the button that actually fired
// (`event.submitter`); sibling submit buttons are disabled right away.
// The submitter itself is disabled a tick later: a synchronous
// `disabled = true` inside the submit event would drop the submitter's
// name/value pair (e.g. `idk=1`) from the POST body, because disabled
// controls are excluded when the browser builds the form entry list
// right after this event returns. `.is-loading` carries
// `pointer-events: none`, so the submitter is already tap-proof
// during that one-tick window.
//
// Safe to call repeatedly (e.g. after a client-side DOM swap brings in
// a fresh form — see trivia-card.js): already-wired forms are skipped.

export function attachDeclarative(root = document) {
  root.querySelectorAll("form[data-submit-pending]").forEach((form) => {
    if (form.dataset.submitPendingBound) return;
    form.dataset.submitPendingBound = "1";
    const buttons = Array.from(form.querySelectorAll("button[type=submit]"));
    if (!buttons.length) return;
    const originals = new Map(
      buttons
        .filter((b) => b.dataset.pendingLabel)
        .map((b) => [b, b.textContent.trim()]),
    );
    form.addEventListener("submit", (e) => {
      const submitter =
        e.submitter && buttons.includes(e.submitter) ? e.submitter : buttons[0];
      buttons.forEach((b) => {
        if (b !== submitter) b.disabled = true;
      });
      if (submitter.dataset.pendingLabel) {
        submitter.textContent = submitter.dataset.pendingLabel;
      }
      submitter.classList.add("is-loading");
      setTimeout(() => {
        submitter.disabled = true;
      }, 0);
    });
    window.addEventListener("pageshow", () => {
      // Listeners for swapped-out forms no-op here; they're released
      // with the page, not worth a teardown protocol.
      if (!form.isConnected) return;
      buttons.forEach((b) => {
        b.disabled = false;
        if (originals.has(b)) b.textContent = originals.get(b);
        b.classList.remove("is-loading");
      });
    });
  });
}
