// trivia-card — behaviors on the trivia card page (standalone +
// session). What remains here is the genuinely-imperative work that
// htmx / the shared submit-pending module can't do declaratively:
//
//   - autoFocusInput on (re-)bind so the textbox is selected without a
//     manual tap.
//   - Pre-fetch the next session card's HTML while the user reads the
//     result panel, so on tap we can swap the DOM synchronously inside
//     the user-gesture window and .focus() the new input — that's what
//     makes iOS show the keyboard automatically. Async focus calls (or
//     navigation-based focus) get swallowed by iOS's input-mode
//     restrictions.
//
// Submit-pending state on the answer + regrade buttons is handled by
// the shared submit-pending module (auto-attached in app.js) via the
// `data-submit-pending` attribute on the forms — no per-page binding.
//
// Standalone /trivia/<qid> deep links use the default navigation;
// no Next-card / keyboard concern there.

let prefetchedHtml = null;
let prefetchedUrl = null;

function autoFocusInput() {
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
  bindNextCardSwap();
  prefetchNextCard();
  autoFocusInput();
}

export function init() {
  window.addEventListener("popstate", onPopState);
  bindAll();
}
