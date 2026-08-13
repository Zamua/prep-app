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
import {el, icon} from "./dom.js";

const ROOT_PATH = new URL(import.meta.url).pathname.replace(/\/static\/js\/.*$/, "");

// Autosave cadence for the in-progress answer. Long enough that
// ordinary typing does not chatter at the server, short enough that a
// crash loses a sentence rather than a paragraph.
const DRAFT_DEBOUNCE_MS = 1200;

const state = {
  mount: null,
  source: null,
  deck: null,
  deckId: null,
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
  // Reaching a real screen means the loop is healthy again, so the
  // auto-recovery budget refills.
  chainedRecoveries = 0;
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

// Errors whose honest recovery is "re-read the session" can chain: the
// re-read hits the same condition, errors again, and recovers again.
// Consecutive auto-recoveries are capped so a server stuck in one
// state produces a dead end with an explanation instead of a spinning
// loop. Any screen the user actually reaches resets the count.
const MAX_CHAINED_RECOVERIES = 3;
let chainedRecoveries = 0;

function recover() {
  if (chainedRecoveries >= MAX_CHAINED_RECOVERIES) {
    chainedRecoveries = 0;
    showDeadEnd();
    return;
  }
  chainedRecoveries += 1;
  showNext();
}

// Terminal screen for a loop that cannot continue. Deliberately not a
// toast: the user needs a way out, and every automatic retry is spent.
function showDeadEnd() {
  cancelPending();
  const section = el("section", "empty-state");
  section.appendChild(el("h2", "empty-headline", "Studying is stuck."));
  section.appendChild(
    el(
      "p",
      "empty-sub",
      "Something on the server keeps interrupting this session. " +
        "Your progress so far is saved."
    )
  );
  const actions = el("div", "study-actions caughtup-actions");
  const retry = el("button", "btn btn-primary", "Try again");
  retry.type = "button";
  retry.addEventListener("click", () => {
    chainedRecoveries = 0;
    showNext();
  });
  const back = el("button", "btn btn-quiet", "Back to deck");
  back.type = "button";
  back.addEventListener("click", onPause);
  actions.appendChild(retry);
  actions.appendChild(back);
  section.appendChild(actions);
  state.mount.replaceChildren(section);
}

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
      recover();
      return;
    case "not_found":
      toast("That card is gone. Loading the next one.", "info");
      recover();
      return;
    case "grading_timeout":
      // The workflow may still land; the card stays in 'grading' until
      // it does, so re-reading is the right move.
      toast("Grading is taking too long. Try again in a moment.", "error");
      recover();
      return;
    case "grading_failed":
      // No verdict is coming. The answer was NOT recorded, so say so
      // and put the card back rather than implying it counted. The
      // grader's own message (why it failed, sometimes with what to
      // do about it) beats a generic line.
      toast(
        e.message
          ? `Grading failed: ${e.message}. Your answer was not recorded.`
          : "The grader failed. Your answer was not recorded.",
        "error"
      );
      recover();
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
  // Authoring files into the deck being studied; the card is where
  // its id comes from.
  if (card && card.deck_id) state.deckId = card.deck_id;
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

// The "Explore further" popover. The server composes the prefilled
// provider URLs (the message embeds the whole card context and the URL
// templates are server config), so the host only renders them. Same
// markup and classes the result page used, so discuss.css still applies.
function handoffNode(handoff) {
  if (!handoff || !handoff.urls) return null;
  const providers = handoff.providers || {};
  const keys = ["claude", "chatgpt"].filter((k) => handoff.urls[k]);
  if (!keys.length) return null;

  const details = el("details", "discuss");
  const summary = el("summary", "discuss-trigger");
  summary.appendChild(el("span", "discuss-label", "Explore further"));
  const caret = el("span", "discuss-caret");
  caret.appendChild(icon("caret-down"));
  summary.appendChild(caret);
  details.appendChild(summary);

  const menu = el("div", "discuss-menu");
  menu.setAttribute("role", "menu");
  for (const key of keys) {
    const a = document.createElement("a");
    a.className = "discuss-option";
    a.setAttribute("role", "menuitem");
    a.href = handoff.urls[key];
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.appendChild(el("span", "discuss-option-name", providers[key] || key));
    const arrow = el("span", "discuss-option-arrow");
    arrow.appendChild(icon("arrow-up-right"));
    a.appendChild(arrow);
    menu.appendChild(a);
  }

  // Not every chat app honours a prefilled universal link, so the
  // message stays copyable as a fallback.
  if (handoff.message) {
    const copy = el("button", "discuss-option discuss-copy");
    copy.type = "button";
    copy.setAttribute("role", "menuitem");
    const label = el("span", "discuss-option-name", "Copy prompt");
    copy.appendChild(label);
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(handoff.message);
        label.textContent = "Copied";
      } catch (e) {
        label.textContent = "Copy failed";
      }
    });
    menu.appendChild(copy);
  }
  details.appendChild(menu);
  return details;
}

function showVerdict(outcome) {
  const card = outcome.card || state.card;
  const handoff = handoffNode(outcome.handoff);
  render(
    verdictView(
      card,
      outcome.verdict,
      outcome.answer || "",
      {
        minutes: outcome.nextDueMinutes,
        idk: outcome.idk,
        feedback: outcome.feedback || null,
        extras: handoff ? [handoff] : [],
      },
      {onNext: showNext, onPause}
    )
  );
}

function showSelfGrade(outcome) {
  const card = outcome.card || state.card;
  // `answer` is what the user wrote. Never fall back to card.answer:
  // an empty submission would then show the model answer back as the
  // user's own words. It also has to survive into the verdict submit,
  // which is what lands in the review log.
  const written = typeof outcome.answer === "string" ? outcome.answer : "";
  render(
    revealView(card, written, {
      blurb:
        "No deterministic grader applies to this card, so you're the " +
        "judge. Mark it honestly. The scheduler works either way.",
      onVerdict: (verdict) => submit(card, {verdict, answer: written}),
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
    // The deck being studied is the only sensible destination here, so
    // it is the picker's single option. Without it the card would file
    // into the inbox and the very next read could not serve it back.
    authorView(
      {decks: state.deckId ? [{id: state.deckId, name: state.deck}] : []},
      {
        blurb: state.deckId
          ? "Added to this deck now. It studies as a " +
            "reveal-and-self-grade card and is due immediately."
          : "Added to your inbox deck. It studies as a " +
            "reveal-and-self-grade card and is due immediately.",
        onSave: async (input) => {
          await state.source.author({...input, deck_id: input.deck_id ?? state.deckId ?? null});
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
