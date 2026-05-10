// delete-deck-dialog — typed-name confirmation for destructive deck
// delete. Modal opens via the trigger button; the submit button is
// disabled until the user types the deck name exactly. Backdrop
// close is wired by the shared dialog module (data-dialog).
//
// Usage:
//   import {init} from "@/modules/delete-deck-dialog.js";
//   init({expectedName: "go-systems"});

export function init({expectedName} = {}) {
  const openBtn = document.getElementById("open-delete-dialog");
  const dlg = document.getElementById("delete-deck-dialog");
  const cancelBtn = document.getElementById("cancel-delete");
  const submitBtn = document.getElementById("delete-submit");
  const input = document.getElementById("delete-confirm-input");
  if (!openBtn || !dlg) return;

  openBtn.addEventListener("click", () => {
    if (input) input.value = "";
    if (submitBtn) submitBtn.disabled = true;
    dlg.showModal();
    if (input) input.focus();
  });

  if (cancelBtn) {
    cancelBtn.addEventListener("click", (e) => {
      e.preventDefault();
      dlg.close();
    });
  }

  if (input && submitBtn && expectedName) {
    input.addEventListener("input", () => {
      submitBtn.disabled = input.value !== expectedName;
    });
  }
}
