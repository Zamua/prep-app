// Disable + label-swap a submit button on form submit so a double-tap
// doesn't fire two POSTs while the server kicks off a workflow + 303s
// to a polling page. Re-enables on `pageshow` if the user back-
// navigates to the form (e.g. after a server-side validation 400).
//
// Declarative usage:
//   <form data-submit-pending>
//     <button type="submit" data-pending-label="Generating…">Submit</button>
//   </form>

export function attachDeclarative(root = document) {
  root.querySelectorAll("form[data-submit-pending]").forEach((form) => {
    const btn = form.querySelector("button[type=submit]");
    if (!btn) return;
    const original = btn.textContent.trim();
    const pending = btn.dataset.pendingLabel || "Working…";
    form.addEventListener("submit", () => {
      btn.disabled = true;
      btn.textContent = pending;
      btn.classList.add("is-loading");
    });
    window.addEventListener("pageshow", () => {
      btn.disabled = false;
      btn.textContent = original;
      btn.classList.remove("is-loading");
    });
  });
}
