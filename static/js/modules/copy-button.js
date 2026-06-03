// Tiny clipboard helper. Any element marked `data-copy-text="…"`
// becomes a click target that copies that text to the clipboard.
// On success the button briefly shows its `data-copy-done-label`
// (default "Copied"), restoring the original label after ~1.5s.
//
// Used by typed-name delete confirmations so users can paste the
// expected string rather than typing it character-by-character.

const DONE_MS = 1500;

export function attachDeclarative(root = document) {
  for (const btn of root.querySelectorAll("[data-copy-text]")) {
    if (btn.dataset.wired) continue;
    btn.dataset.wired = "1";
    btn.addEventListener("click", (e) => handleClick(e, btn));
  }
}

async function handleClick(event, btn) {
  event.preventDefault();
  const text = btn.dataset.copyText;
  if (!text) return;

  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // Older browsers / iframes without clipboard permission. Fall
    // back to a temporary textarea + execCommand("copy").
    fallbackCopy(text);
  }

  const doneLabel = btn.dataset.copyDoneLabel || "Copied";
  const original = btn.innerHTML;
  // Lock width so the label swap doesn't reflow neighbors (mobile
  // UX rail — no layout shift on interaction).
  const width = btn.getBoundingClientRect().width;
  btn.style.minWidth = `${width}px`;
  btn.textContent = doneLabel;
  btn.classList.add("is-copied");

  setTimeout(() => {
    btn.innerHTML = original;
    btn.classList.remove("is-copied");
    btn.style.minWidth = "";
  }, DONE_MS);
}

function fallbackCopy(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.position = "absolute";
  ta.style.left = "-9999px";
  document.body.appendChild(ta);
  ta.select();
  try {
    document.execCommand("copy");
  } catch {
    /* swallow — best-effort */
  }
  document.body.removeChild(ta);
}
