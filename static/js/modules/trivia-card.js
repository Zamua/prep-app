// trivia-card — behaviors on the trivia card page (standalone +
// session). One consolidated entry point that binds on initial page
// load AND re-binds after an in-place session-card swap (since
// innerHTML doesn't execute embedded scripts).
//
// Highlights:
// - Submit-pending state on answer + regrade buttons.
// - Pre-fetch the next session card's HTML in the background while
//   the user reads the result panel.
// - On Next-card tap, synchronously swap the DOM and .focus() the
//   new input — staying inside the user-gesture window so iOS shows
//   the keyboard automatically (otherwise iOS swallows .focus calls
//   that follow async work).
// - Standalone /trivia/<qid> deep links use the default navigation;
//   no Next-card / keyboard concern there.

let prefetchedHtml = null;
let prefetchedUrl = null;

function bindSubmitPending() {
  const btn = document.getElementById("trivia-submit-btn");
  if (!btn || btn.dataset.boundPending) return;
  btn.dataset.boundPending = "1";
  const orig = btn.textContent.trim();
  const pending = btn.dataset.pendingLabel || "Working…";
  btn.form.addEventListener("submit", () => {
    btn.disabled = true;
    btn.textContent = pending;
    btn.classList.add("is-loading");
  });
  window.addEventListener("pageshow", () => {
    btn.disabled = false;
    btn.textContent = orig;
    btn.classList.remove("is-loading");
  });
}

function bindRegradePending() {
  const btn = document.querySelector(".trivia-regrade-btn");
  if (!btn || btn.dataset.boundPending) return;
  btn.dataset.boundPending = "1";
  btn.form.addEventListener("submit", () => {
    btn.disabled = true;
    btn.classList.add("is-loading");
  });
}

function autoFocusInput() {
  // Mid-session input — focus on (re-)bind so the cursor lands
  // there without a manual tap. iOS keyboard appears only when
  // .focus() is called inside a live user-gesture (handled in
  // bindNextCardSwap below); this autofocus path covers desktop /
  // Android and is harmless on iOS.
  const input = document.querySelector(".trivia-answer-input");
  if (input) {
    try {
      input.focus();
    } catch (e) {
      /* ignore */
    }
  }
}

function prefetchNextCard() {
  const nextLink = document.querySelector(".trivia-next-cta");
  if (!nextLink || nextLink.tagName !== "A") return;
  // Only session navigation (with session_remaining cards) is worth
  // pre-fetching. The standalone "Dismiss" link goes home — no
  // input to focus there.
  if (!nextLink.href.match(/\/trivia\/session\//)) return;
  const url = nextLink.href;
  if (url === prefetchedUrl) return;
  prefetchedUrl = url;
  prefetchedHtml = null;
  fetch(url, {credentials: "same-origin"})
    .then((r) => (r.ok ? r.text() : null))
    .then((html) => {
      if (prefetchedUrl === url) prefetchedHtml = html;
    })
    .catch(() => {
      /* fall through to default nav on click */
    });
}

function bindNextCardSwap() {
  const nextLink = document.querySelector(".trivia-next-cta");
  if (!nextLink || nextLink.tagName !== "A" || nextLink.dataset.boundSwap) return;
  if (!nextLink.href.match(/\/trivia\/session\//)) return;
  nextLink.dataset.boundSwap = "1";
  nextLink.addEventListener("click", (e) => {
    if (!prefetchedHtml || prefetchedUrl !== nextLink.href) {
      // Pre-fetch hasn't landed — fall through to default navigation.
      // Keyboard won't show on iOS this once; subsequent cards fine.
      return;
    }
    e.preventDefault();
    const doc = new DOMParser().parseFromString(prefetchedHtml, "text/html");
    const newMain = doc.querySelector("main.folio");
    const curMain = document.querySelector("main.folio");
    if (!newMain || !curMain) {
      window.location.href = nextLink.href;
      return;
    }
    curMain.innerHTML = newMain.innerHTML;
    history.pushState(null, "", nextLink.href);
    document.title = doc.title;
    // Synchronously focus the new input — still inside the tap's
    // user-gesture window because no async/await crossed between
    // the click and this call (the prefetched HTML is already in
    // memory). iOS shows keyboard.
    autoFocusInput();
    prefetchedHtml = null;
    prefetchedUrl = null;
    bindAll();
  });
}

function onPopState() {
  // Browser back/forward landed on a previous URL — let the
  // browser do its bfcache thing. Fallback to full reload if
  // the state machine gets confused.
  window.location.reload();
}

function bindAll() {
  bindSubmitPending();
  bindRegradePending();
  bindNextCardSwap();
  prefetchNextCard();
  autoFocusInput();
}

export function init() {
  window.addEventListener("popstate", onPopState);
  bindAll();
}
