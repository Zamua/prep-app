// online-host.js: the signed-in study page. Wires ServerSource to the
// shared study views, owns the mount, navigation, and error surfaces.
// The offline shell (offline/offline-app.js) is the same wiring over
// LocalSource; the views themselves are shared and know neither host.

import {
  authorView,
  caughtUpView,
  pendingView,
  revealView,
  setActionErrorHandler,
  studyCardView,
  verdictView,
} from "./components.js";
import {ServerSource, StudySourceError} from "./source.js";

const ROOT_PATH = new URL(import.meta.url).pathname.replace(/\/static\/js\/.*$/, "");

// Autosave cadence for the in-progress answer. Long enough that
// ordinary typing does not chatter at the server, short enough that a
// crash loses a sentence rather than a paragraph.
const DRAFT_DEBOUNCE_MS = 1200;

const state = {
  mount: null,
  source: null,
  deck: null,
  deckHref: null,
  signInUrl: null,
  card: null,
  draftTimer: null,
  pending: null,
};

// ---- chrome ----------------------------------------------------------

function toast(message, kind = "info") {
  const host = document.querySelector("[data-study-toast]");
  if (!host) return;
  host.textContent = message;
  host.className = "study-toast toast-" + kind;
  host.hidden = false;
  window.setTimeout(() => {
    host.hidden = true;
  }, 4000);
}

function render(node) {
  cancelPending();
  state.mount.replaceChildren(node);
  window.scrollTo({top: 0});
}

// A pending grade belongs to the screen that started it. Leaving that
// screen stops the loop so a late resolution cannot overwrite whatever
// the user moved on to.
function cancelPending() {
  if (state.pending) {
    state.pending.cancel();
    state.pending = null;
  }
}

function flushDraftTimer() {
  if (state.draftTimer) {
    window.clearTimeout(state.draftTimer);
    state.draftTimer = null;
  }
}

// ---- error policy ----------------------------------------------------

// Every failure that reaches a user lands here. Recoverable errors
// toast and leave the screen usable; identity loss navigates, because
// nothing on the page can work without a session.
function handleError(e) {
  if (!(e instanceof StudySourceError)) {
    console.warn("study action failed:", e);
    toast("Something went wrong. Try again.", "error");
    return;
  }
  switch (e.code) {
    case "unauthorized":
      // The cookie died under the page. Bounce to sign-in rather than
      // leaving a surface whose every button will fail.
      window.location.assign(state.signInUrl || ROOT_PATH + "/");
      return;
    case "stale_version":
      // Another device moved this session on. The server already told
      // the source the current version; re-reading is the only honest
      // recovery, so never silently resubmit.
      toast("This session moved on another device. Catching up.", "info");
      showNext();
      return;
    case "not_found":
      toast("That card is gone. Loading the next one.", "info");
      showNext();
      return;
    case "grading_timeout":
      toast("Grading is taking too long. Your answer was recorded.", "error");
      showNext();
      return;
    case "grading_failed":
      toast("Grading failed. Your answer was recorded.", "error");
      showNext();
      return;
    case "network":
      toast("Network trouble. Check your connection and try again.", "error");
      return;
    case "cancelled":
      return;
    default:
      console.warn("study action failed:", e);
      toast("Something went wrong. Try again.", "error");
  }
}

// ---- views -----------------------------------------------------------

function onPause() {
  flushDraftTimer();
  cancelPending();
  window.location.assign(state.deckHref);
}

function showCard(card, draft) {
  state.card = card;
  render(
    studyCardView(card, {
      draft: draft || "",
      onAnswer: (answer) => submit(card, {answer}),
      onIdk: () => submit(card, {idk: true}),
      onDraft: (text) => queueDraft(text),
      onPause,
    })
  );
}

function showVerdict(outcome) {
  const card = outcome.card || state.card;
  render(
    verdictView(
      card,
      outcome.verdict,
      outcome.answer || "",
      {
        minutes: outcome.nextDueMinutes,
        idk: outcome.idk,
        feedback: outcome.feedback || null,
      },
      {onNext: showNext, onPause}
    )
  );
}

function showSelfGrade(outcome) {
  const card = outcome.card || state.card;
  render(
    revealView(card, outcome.answer || card.answer || "", {
      blurb:
        "No deterministic grader applies to this card, so you're the " +
        "judge. Mark it honestly. The scheduler works either way.",
      onVerdict: (verdict) => submit(card, {verdict, answer: outcome.userAnswer || ""}),
      onPause,
    })
  );
}

function showCaughtUp(summary) {
  render(
    caughtUpView(summary, {
      scope: "here",
      nothingScheduled: "Nothing else is scheduled in this deck.",
      backLabel: "Back to deck",
      onAdd: showAuthor,
      onBack: onPause,
    })
  );
}

function showAuthor() {
  render(
    authorView(
      {decks: []},
      {
        blurb:
          "Added to this deck now. It studies as a " +
          "reveal-and-self-grade card and is due immediately.",
        onSave: async (input) => {
          await state.source.author(input);
          toast("Card added.", "info");
          showNext();
        },
        onBack: showNext,
      }
    )
  );
}

// ---- the loop --------------------------------------------------------

// Branch on the outcome in contract order: a settled verdict and a
// selfGrade both carry `card`, so testing `card` first would render a
// finished answer as a fresh question (source.js documents the order).
function apply(view) {
  if (view.pending) return showPending(view.pending);
  if (view.verdict) return showVerdict(view);
  if (view.selfGrade) return showSelfGrade(view);
  if (view.ended) return onPause();
  if (view.caughtUp) return showCaughtUp(view.caughtUp);
  if (view.card) return showCard(view.card, view.draft);
  toast("Nothing to study right now.", "info");
}

function showPending(pending) {
  const card = state.card;
  state.mount.replaceChildren(pendingView(card, pending, {onPause}));
  state.pending = pending;
  pending.settled.then(
    (outcome) => {
      if (state.pending !== pending) return;
      state.pending = null;
      apply(outcome);
    },
    (e) => {
      if (state.pending !== pending) return;
      state.pending = null;
      handleError(e);
    }
  );
}

async function submit(card, submission) {
  flushDraftTimer();
  try {
    apply(await state.source.submit(card, submission));
  } catch (e) {
    handleError(e);
  }
}

async function showNext() {
  try {
    apply(await state.source.next());
  } catch (e) {
    handleError(e);
  }
}

function queueDraft(text) {
  if (!state.source.session) return;
  flushDraftTimer();
  state.draftTimer = window.setTimeout(() => {
    state.draftTimer = null;
    // A failed autosave is not worth interrupting typing for: the
    // answer still submits from the field the user is looking at.
    state.source.saveDraft(text).catch(() => {});
  }, DRAFT_DEBOUNCE_MS);
}

// ---- boot ------------------------------------------------------------

export async function init() {
  const mount = document.querySelector("[data-study-root]");
  if (!mount) return;
  state.mount = mount;
  state.deck = mount.dataset.deck || null;
  state.signInUrl = mount.dataset.signInUrl || null;
  state.deckHref = state.deck
    ? ROOT_PATH + "/deck/" + encodeURIComponent(state.deck)
    : ROOT_PATH + "/";
  const sessionId = mount.dataset.sessionId || null;
  state.source = new ServerSource({deck: state.deck, sessionId});
  setActionErrorHandler(handleError);

  try {
    // A session id in the shell resumes it; a deck alone opens or
    // resumes that deck's session.
    apply(sessionId ? await state.source.next() : await state.source.begin());
  } catch (e) {
    handleError(e);
  }
}

init();
