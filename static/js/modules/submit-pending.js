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

export function attachDeclarative(root = document) {
  root.querySelectorAll("form[data-submit-pending]").forEach((form) => {
    const btn = form.querySelector("button[type=submit]");
    if (!btn) return;
    const explicitPending = btn.dataset.pendingLabel;
    const original = explicitPending ? btn.textContent.trim() : null;
    form.addEventListener("submit", () => {
      btn.disabled = true;
      if (explicitPending) btn.textContent = explicitPending;
      btn.classList.add("is-loading");
    });
    window.addEventListener("pageshow", () => {
      btn.disabled = false;
      if (explicitPending && original !== null) btn.textContent = original;
      btn.classList.remove("is-loading");
    });
  });
}
