// Manual <details> toggle bound to pointerup, with Esc + outside-click
// close. iOS 26 in PWA standalone mode swallows the synthesized click
// event on <summary> for the first ~5s after page load (Apple radar
// #159814). Pointer events fire reliably; we own the toggle in JS.
//
// Behavior:
//  - capture-phase pointerup on document toggles the parent <details>
//    open attribute, then preventDefault'd to suppress the iOS-late
//    compatibility click that would otherwise double-toggle.
//  - 500ms gate suppresses the late click; keyboard activation
//    (Enter/Space → click without preceding pointerup) lands outside
//    the gate and falls through to native activation, so keyboard
//    accessibility isn't broken.
//  - outside-pointerup closes any open <details> (skipping clicks on
//    a summary, which the toggler above already handled).
//  - Escape closes any open <details> + open <dialog>.

const COMPAT_CLICK_WINDOW_MS = 500;

export function init() {
  let lastSummaryToggle = 0;

  document.addEventListener(
    "pointerup",
    (e) => {
      const summary = e.target.closest && e.target.closest("summary");
      if (!summary) return;
      const details = summary.parentElement;
      if (!details || details.tagName !== "DETAILS") return;
      e.preventDefault();
      if (details.hasAttribute("open")) details.removeAttribute("open");
      else details.setAttribute("open", "");
      lastSummaryToggle = Date.now();
      summary.blur && summary.blur();
    },
    true,
  );

  document.addEventListener(
    "click",
    (e) => {
      if (Date.now() - lastSummaryToggle > COMPAT_CLICK_WINDOW_MS) return;
      if (e.target.closest && e.target.closest("summary")) {
        e.preventDefault();
        e.stopPropagation();
      }
    },
    true,
  );

  // Outside-pointerup → close open details. Skip when:
  //   - target is a summary (the toggler above handled it).
  //   - target is inside [data-details-body] (sibling popover bodies;
  //     e.g. trivia card explore/explain — they're children of
  //     .trivia-discs, not the <details> itself).
  //   - target is an actionable element (link, button, role=button,
  //     submit input). Closing details on the SAME pointerup that
  //     would fire a link click shifts layout on iOS just before the
  //     synthesized click resolves, and the click either cancels or
  //     re-targets to the now-shifted element underneath. Symptom is
  //     "tap registers but nothing happens; second tap works"
  //     (e.g. tap Next-card while Explain is open: Explain closes,
  //     no nav; tap "all decks" while interval popover is open:
  //     popover closes, no nav).
  //
  // Net behavior: open details persist through taps on actions until
  // the user re-taps the summary (toggle), presses Escape, or taps a
  // genuinely-non-actionable region of the page.
  document.addEventListener("pointerup", (e) => {
    if (e.target.closest && e.target.closest("summary")) return;
    if (e.target.closest && e.target.closest("[data-details-body]")) return;
    if (e.target.closest && e.target.closest("a, button, [role='button'], input[type='submit']")) {
      return;
    }
    document.querySelectorAll("details[open]").forEach((d) => {
      if (!d.contains(e.target)) d.removeAttribute("open");
    });
  });

  // Esc closes open details + open dialogs.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    document.querySelectorAll("details[open]").forEach((d) => d.removeAttribute("open"));
    document.querySelectorAll("dialog[open]").forEach((d) => d.close && d.close());
  });
}
