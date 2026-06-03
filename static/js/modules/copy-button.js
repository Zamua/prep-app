// Tiny clipboard helper. Any element marked `data-copy-text="…"`
// becomes a click target that copies that text to the clipboard.
// On success the `.is-copied` class is added for ~1.5s; the
// template renders both the default + done icons inside the button
// and CSS toggles which one is visible based on the class.
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

  btn.classList.add("is-copied");
  setTimeout(() => btn.classList.remove("is-copied"), DONE_MS);
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
