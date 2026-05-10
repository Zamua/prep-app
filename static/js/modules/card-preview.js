// card-preview — click any qcard on the deck page to open a modal
// that previews the rendered card. The card carries its rendered
// HTML in a hidden <template class="qcard-preview-tpl">; on click we
// clone that into the dialog's body.
//
// Skip clicks that landed on an interactive descendant (suspend,
// improve, forms, summary, etc.) — those have their own handlers
// and shouldn't trigger preview. Same for keyboard activation
// (Enter / Space) on tabindex=0 cards.
//
// Backdrop click + Esc close are handled by the shared dialog module
// (static/js/modules/dialog.js, hooked via data-dialog on the dialog
// element).
//
// Usage:
//   import {init} from "@/modules/card-preview.js";
//   init();

const INTERACTIVE_SELECTOR =
  "form, button, summary, a, input, textarea, .qcard-actions, .qcard-foot";

export function init() {
  const dlg = document.getElementById("card-preview");
  const body = document.getElementById("card-preview-body");
  if (!dlg || !body) return;

  function openPreview(card) {
    const tpl = card.querySelector(".qcard-preview-tpl");
    if (!tpl) return;
    body.innerHTML = "";
    body.appendChild(tpl.content.cloneNode(true));
    body.scrollTop = 0;
    dlg.showModal();
  }

  document.querySelectorAll(".qcard").forEach((card) => {
    card.addEventListener("click", (e) => {
      if (e.target.closest(INTERACTIVE_SELECTOR)) return;
      openPreview(card);
    });
    card.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      if (e.target.closest("form, button, summary, a, input, textarea")) return;
      e.preventDefault();
      openPreview(card);
    });
  });
}
