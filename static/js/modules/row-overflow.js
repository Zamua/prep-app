// Per-row overflow-menu coordinator.
//
// Solves two issues for the .row-overflow-menu pattern used by deck
// cards (index page) and session cards (continue-trivia strip):
//
//   1. Mutual exclusion. Opening one menu auto-closes any other open
//      .row-overflow-menu. Tapping a second card's ⋯ should swap
//      menus, not stack popovers.
//
//   2. Z-index lift. The parent card (.deck-card, .session-card) has
//      a rise-animation post-state of transform: translateY(0), which
//      creates a stacking context. The :has() selector-based lift
//      lands on chromium but iOS Safari can compute the stacking
//      order differently for adjacent transformed siblings, so we
//      tag the open card with an `is-overflow-open` class JS-side
//      and the CSS lifts that one explicitly.
//
// Native `toggle` event fires on the <details> whenever its open
// state flips — no polling, no MutationObserver needed.

const MENU_SELECTOR = ".row-overflow-menu";
const CARD_PARENTS = ".deck-card, .session-card";
const OPEN_CARD_CLASS = "is-overflow-open";

export function init() {
  document.addEventListener(
    "toggle",
    (e) => {
      const details = e.target;
      if (!details || !details.matches || !details.matches(MENU_SELECTOR)) return;
      const card = details.closest(CARD_PARENTS);

      if (details.open) {
        // 1) Close any OTHER open menus so only this one is visible.
        document.querySelectorAll(`${MENU_SELECTOR}[open]`).forEach((other) => {
          if (other !== details) other.removeAttribute("open");
        });
        // 2) Lift this card above its siblings.
        if (card) card.classList.add(OPEN_CARD_CLASS);
      } else if (card) {
        card.classList.remove(OPEN_CARD_CLASS);
      }
    },
    true, // capture: toggle doesn't bubble, capture-phase is the only reliable way to delegate at document
  );
}
