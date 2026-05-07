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

  // Outside-pointerup → close open details. Skip when the target is
  // a summary (the toggler above already handled it). Skip when the
  // target is inside a [data-details-body] element — those are body
  // panels rendered as a sibling of the <details> for layout reasons
  // (see trivia-card.html: explore body lives as a .trivia-discs
  // child, not a <details> child, so the row of pills can grid
  // independently of the body's full-width panel). Without this
  // exemption, tapping a link in the body would close the details
  // mid-tap and on iOS the synthesized click would never reach the
  // <a>'s navigation.
  document.addEventListener("pointerup", (e) => {
    if (e.target.closest && e.target.closest("summary")) return;
    if (e.target.closest && e.target.closest("[data-details-body]")) return;
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
