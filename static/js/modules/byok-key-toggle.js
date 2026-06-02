/* Peek-toggle for BYOK key inputs on /settings/agent.
 *
 * Flips a single `<input>` between type="password" (default) and
 * type="text" so the user can verify a paste without leaving the
 * secret visible on screen. The eye icon swaps via CSS based on
 * the button's data-state attribute.
 *
 * Declarative: any `.byok-key-toggle` button gets wired up. No
 * per-page setup; just include this module in the bootstrap.
 */

export function attachDeclarative(root = document) {
  for (const btn of root.querySelectorAll(".byok-key-toggle")) {
    if (btn.dataset.wired) continue;
    btn.dataset.wired = "1";
    btn.addEventListener("click", () => {
      const input = btn.parentElement?.querySelector("input");
      if (!input) return;
      const showing = btn.dataset.state === "shown";
      if (showing) {
        input.type = "password";
        btn.dataset.state = "hidden";
        btn.setAttribute("aria-label", "Show key");
      } else {
        input.type = "text";
        btn.dataset.state = "shown";
        btn.setAttribute("aria-label", "Hide key");
      }
    });
  }
}
