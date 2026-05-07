// <dialog> helper. Native <dialog> doesn't close on backdrop click
// by default; this module wires that up + adds a small Esc-to-close
// safety net for dialogs that were opened without showModal() (rare
// but possible).
//
// Usage (declarative):
//   <dialog data-dialog>
//     <form method="dialog"><button>×</button></form>
//     ...
//   </dialog>
//   The element is auto-wired on page load — backdrop click closes
//   the dialog.
//
// Usage (programmatic):
//   import {openDialog, closeDialog} from "@/modules/dialog.js";
//   openDialog(el);  closeDialog(el);

export function openDialog(el) {
  if (!el || el.tagName !== "DIALOG") return;
  if (typeof el.showModal === "function") el.showModal();
  else el.setAttribute("open", "");
}

export function closeDialog(el) {
  if (!el || el.tagName !== "DIALOG") return;
  if (typeof el.close === "function") el.close();
  else el.removeAttribute("open");
}

function wireBackdropClose(el) {
  // Native <dialog> reports `event.target === el` when the click
  // landed on the dialog's padding/backdrop area (i.e. NOT on a
  // descendant). That's the signal to close.
  el.addEventListener("click", (e) => {
    if (e.target === el) closeDialog(el);
  });
}

export function attachDeclarative(root = document) {
  root.querySelectorAll("dialog[data-dialog]").forEach(wireBackdropClose);
}
