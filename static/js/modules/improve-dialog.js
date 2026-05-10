// improve-dialog — one shared <dialog id="improve-dialog"> on the
// deck page that any qcard's "Improve with claude" button can open.
// Clicking the button populates the dialog's form action +
// card-context preview, then showModal()s. Submission is a real
// POST so the server can kick off the transform workflow and
// redirect to /transform/<wid>.
//
// Per-card data flows in via dataset attrs on the trigger button:
//   data-qid="123"             — the question id (used for the action)
//   data-prompt="…"            — the card's rendered prompt (preview)
//
// Usage:
//   import {init} from "@/modules/improve-dialog.js";
//   init({rootPath: "/prep"});  // rootPath comes from the template

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[c]));
}

export function init({rootPath = ""} = {}) {
  const dlg = document.getElementById("improve-dialog");
  const form = document.getElementById("improve-form");
  const ctx = document.getElementById("improve-card-context");
  const promptInput = document.getElementById("improve-prompt");
  const cancel = document.getElementById("improve-cancel");
  if (!dlg || !form) return;

  document.querySelectorAll(".qcard-action--improve").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      const qid = btn.dataset.qid;
      const cardPrompt = btn.dataset.prompt || "";
      form.action = `${rootPath}/question/${qid}/improve`;
      if (ctx) {
        ctx.innerHTML =
          `<span class="improve-card-num">№ ${qid}</span> ` +
          escapeHtml(cardPrompt) +
          (cardPrompt.length >= 200 ? "…" : "");
      }
      if (promptInput) promptInput.value = "";
      dlg.showModal();
      // Focus the prompt after showModal so iOS pops the keyboard.
      requestAnimationFrame(() => promptInput && promptInput.focus());
    });
  });

  if (cancel) cancel.addEventListener("click", () => dlg.close());
}
