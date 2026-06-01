// Per-row overflow-menu coordinator.
//
// Two responsibilities:
//
//   1. Mutual exclusion. Opening one .row-overflow-menu auto-closes
//      any other open one. Without this, tapping a second card's ⋯
//      would leave the first menu open.
//
//   2. Z-index lift. When a menu opens, the parent card (.deck-card
//      / .session-card) gets `.is-overflow-open` so CSS can lift it
//      above its siblings.
//
//      Why this is needed even after the rise→fade-in change: the
//      :hover transform on .deck-card (translateY(-2px), the nice
//      lift-on-hover affordance) creates a transient stacking
//      context when the user taps the ⋯ trigger — touch devices
//      hold :hover briefly on tap. With the open card as a stacking
//      context at z-index auto, both it AND its non-stacking sibling
//      cards paint in step 6 of the parent's painting order, in
//      tree order — the next sibling, later in tree order, paints
//      OVER the open card's popover.
//
//      Lifting the open card to z-index >= 1 promotes it to step 7
//      (z-index > 0), which paints AFTER step 6 unconditionally.
//      The popover (descendant of the lifted stacking context) wins.
//
// Native `toggle` event fires on the <details> whenever its open
// state flips. We listen capture-phase since toggle doesn't bubble.

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
        // Mutex: close any other open menu.
        document.querySelectorAll(`${MENU_SELECTOR}[open]`).forEach((other) => {
          if (other !== details) other.removeAttribute("open");
        });
        if (card) card.classList.add(OPEN_CARD_CLASS);
      } else if (card) {
        card.classList.remove(OPEN_CARD_CLASS);
      }
    },
    true,
  );
}
