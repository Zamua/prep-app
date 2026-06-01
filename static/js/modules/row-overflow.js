// Per-row overflow-menu coordinator.
//
// Mutual exclusion: opening one .row-overflow-menu auto-closes any
// other open one. Without this, tapping a second card's ⋯ would
// leave the first menu open — the user would have two popovers
// stacked, which is never what they want.
//
// Native `toggle` event fires on the <details> whenever its open
// state flips. We listen capture-phase since toggle doesn't bubble.
//
// Z-index is NOT handled here — see static/css/components/deck-list.css
// for the fix: deck cards use opacity-only `fade-in` instead of the
// transform-based `rise`, so they don't create per-card stacking
// contexts that would trap the popover.

const MENU_SELECTOR = ".row-overflow-menu";

export function init() {
  document.addEventListener(
    "toggle",
    (e) => {
      const details = e.target;
      if (!details || !details.matches || !details.matches(MENU_SELECTOR)) return;
      if (!details.open) return;
      document.querySelectorAll(`${MENU_SELECTOR}[open]`).forEach((other) => {
        if (other !== details) other.removeAttribute("open");
      });
    },
    true,
  );
}
