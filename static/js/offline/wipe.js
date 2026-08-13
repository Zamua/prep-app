// wipe.js: the one safe way to destroy this device's offline data.
// Three paths reach it -- the forget-device form, the sign-out
// choice, and the landing page's removal action -- and all three go
// through the same rules.
//
// Rule one: flush first. `outbox_reviews` holds study and
// `local_cards` holds authoring the server has never seen, and
// store.wipeAll clears them with everything else. So flush, read the
// queues back, and wipe only when they came back empty. The queues
// are the verdict, never flushOutbox's return value: a transient
// failure inside the review loop leaves rows behind and still reports
// the acked count, and an unreadable store answers "unknown", which
// is not "empty". `rejects` counts with them: the server refused
// those rows for good, so no flush can save them and the device is
// their only copy.
//
// Rule two: a flush needs the session. Any caller that also drops
// credentials must finish here BEFORE it navigates.
//
// Rule three: report, never assume. A wipe that did not commit says
// so; a flush that could not save the work names the actual reason,
// because the landing removal always runs without a session and
// "could not reach the server" would be wrong on the one path
// guaranteed to show that dialog.
//
// What survives a wipe lives on the server, so nothing here may say
// the cards are gone from the account: this device is all that is
// cleared.

import {getAll, snapshotFlagSet, wipeAll, withLock} from "./store.js";
import {flushOutbox} from "./sync.js";

// A network round trip the user is waiting on.
const FLUSH_DEADLINE_MS = 10000;

// Storage reads that decide what to say. A device that cannot answer
// in this long is unreadable, which is its own answer.
const READ_DEADLINE_MS = 2000;

const TIMED_OUT = Symbol("timed out");

function withDeadline(promise, ms) {
  let timer = null;
  const deadline = new Promise((resolve) => {
    timer = setTimeout(() => resolve(TIMED_OUT), ms);
  });
  return Promise.race([promise, deadline]).finally(() => clearTimeout(timer));
}

// ---- reads -----------------------------------------------------------

// Whether there is anything on this device worth warning about. The
// flag answers instantly for the common case; a snapshot older than
// the flag is found by reading the stores. The unsynced stores count:
// a device whose snapshot went empty can still hold the last study
// the session is about to stop being able to save. An unreadable
// device answers false: there is nothing truthful to warn about, and
// the warning must never become unconditional friction.
export async function deviceHoldsCards() {
  if (snapshotFlagSet()) return true;
  try {
    const held = await withDeadline(
      Promise.all([
        getAll("decks"),
        getAll("cards"),
        getAll("local_cards"),
        getAll("outbox_reviews"),
        getAll("rejects"),
      ]),
      READ_DEADLINE_MS
    );
    if (held === TIMED_OUT) return false;
    return held.some((rows) => rows.length > 0);
  } catch (e) {
    return false;
  }
}

// Work the server does not have. Null when the stores could not be
// read: callers treat that as "there may be something", so a wipe
// never proceeds on a device that failed to answer.
async function unsyncedCounts() {
  try {
    const rows = await withDeadline(
      Promise.all([getAll("outbox_reviews"), getAll("local_cards"), getAll("rejects")]),
      READ_DEADLINE_MS
    );
    if (rows === TIMED_OUT) return null;
    return {reviews: rows[0].length, cards: rows[1].length, rejects: rows[2].length};
  } catch (e) {
    return null;
  }
}

function anyPending(pending) {
  return !pending || Boolean(pending.reviews || pending.cards || pending.rejects);
}

// ---- the wipe --------------------------------------------------------

// Clear every store without flushing. Reached only from an explicit
// second choice over named unsynced work. False when the wipe did not
// commit or could not be confirmed: a caller that reports success
// here tells someone leaving a shared browser that it is clean.
async function wipeNow() {
  try {
    const done = await withDeadline(
      withLock(() => wipeAll()),
      FLUSH_DEADLINE_MS
    );
    return done !== TIMED_OUT;
  } catch (e) {
    console.warn("offline wipe failed:", e);
    return false;
  }
}

// Why the flush could not save what is still queued. Decides the
// copy, so each value must name a cause the user can act on.
const OFFLINE = "offline";
const SIGNED_OUT = "signed-out";
const OTHER_ACCOUNT = "other-account";
const REFUSED = "refused";
const STORAGE = "storage";

function flushReason(result) {
  if (!result || result === TIMED_OUT) return OFFLINE;
  if (result.status === 401) return SIGNED_OUT;
  if (result.status || result.partial || result.owner_unstamped) return OFFLINE;
  if (result.disabled) return OTHER_ACCOUNT;
  // The flush ran and sent what it could, so anything still queued is
  // a row the server would not take.
  return REFUSED;
}

// Flush, verify, wipe. `wiped` is true only when the queues came back
// empty AND the wipe committed; otherwise `pending` is what the
// caller must put in front of the user and `reason` is why.
async function flushThenWipe() {
  let result = null;
  try {
    result = await withDeadline(flushOutbox(), FLUSH_DEADLINE_MS);
  } catch (e) {
    // A throw is a failed flush. The queue read below decides either way.
  }
  const reason = flushReason(result);
  const pending = await unsyncedCounts();
  if (anyPending(pending)) return {wiped: false, pending, reason};
  const wiped = await wipeNow();
  return {wiped, pending, reason: wiped ? reason : STORAGE};
}

// ---- dialogs ---------------------------------------------------------

// Data reaches a dialog through textContent only; nothing here
// touches innerHTML.
function makeEl(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// A modal with a title, some lines, and a column of choices. Resolves
// with {key, result} for the button pressed, or null when the user
// closed it without choosing (backdrop, Esc). A choice carrying `run`
// keeps the dialog open with its button busy until the work settles.
//
// Same data-dialog convention the templates use; the backdrop wiring
// is inline because these nodes are built long after dialog.js's
// declarative pass ran.
function choose({className, title, lines, choices}) {
  return new Promise((resolve) => {
    // Closing settles the previous promise; removing the node fires no
    // close event and strands whoever is awaiting it. A busy one is
    // left alone: its run is still in flight.
    const previous = document.querySelector("dialog." + className);
    if (previous && !previous.hasAttribute("data-busy")) {
      if (previous.close) previous.close();
      else previous.remove();
    }

    const dialog = document.createElement("dialog");
    dialog.className = "offline-choice-dialog " + className;
    dialog.setAttribute("data-dialog", "");
    const titleId = className + "-title";
    dialog.setAttribute("aria-labelledby", titleId);

    const heading = makeEl("h3", null, title);
    heading.id = titleId;
    dialog.appendChild(heading);
    for (const line of lines) dialog.appendChild(makeEl("p", null, line));

    const actions = makeEl("div", "offline-choice-actions");
    let busy = false;
    let answer = null;

    for (const choice of choices) {
      const button = makeEl("button", choice.className, choice.label);
      button.type = "button";
      button.dataset.choice = choice.key;
      button.addEventListener("click", async () => {
        if (busy) return;
        if (!choice.run) {
          answer = {key: choice.key, result: null};
          dialog.close();
          return;
        }
        busy = true;
        // The marker, not the cancel event, is what holds a mid-flight
        // choice open: a direct .close() from elsewhere fires no cancel
        // at all, and modules/details-toggle.js closes open dialogs on
        // Esc that way. It skips this attribute.
        dialog.setAttribute("data-busy", "");
        button.classList.add("is-loading");
        let result = null;
        try {
          result = await choice.run();
        } catch (e) {
          console.warn("offline wipe step failed:", e);
        }
        busy = false;
        dialog.removeAttribute("data-busy");
        button.classList.remove("is-loading");
        answer = {key: choice.key, result};
        dialog.close();
      });
      actions.appendChild(button);
    }
    dialog.appendChild(actions);

    // Backdrop click and Esc both mean undecided, which is cancel.
    // Neither is offered while a choice is mid-flight.
    dialog.addEventListener("click", (e) => {
      if (e.target === dialog && !busy) dialog.close();
    });
    dialog.addEventListener("cancel", (e) => {
      if (busy) e.preventDefault();
    });
    dialog.addEventListener("close", () => {
      dialog.remove();
      resolve(answer);
    });

    document.body.appendChild(dialog);
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  });
}

// ---- copy ------------------------------------------------------------

const SIGN_OUT_TITLE = "Sign out";

const SIGN_OUT_STAYS =
  "The cards saved on this browser stay here after you sign out. Anyone " +
  "who uses this browser can open them.";

const SIGN_OUT_REMOVES =
  "Removing them clears this browser only. Your account keeps your decks.";

const SIGN_OUT_KEEP_LABEL = "Sign out, keep the cards";

const REMOVE_TITLE = "Remove these cards from this browser?";

const REMOVE_CLEARS =
  "This browser stops showing them. Nothing is deleted from the account " +
  "they came from.";

const UNSYNCED_TITLE = "Some work is not saved";

const UNSYNCED_UNKNOWN =
  "Work you did on this browser may not have reached your account yet.";

const LOST = "Removing the cards deletes it for good.";

const CAUSE_OFFLINE =
  "prep could not reach the server just now, so it cannot save that work. " + LOST;

const CAUSE_SIGNED_OUT =
  "prep cannot save that work because this browser is not signed in. " + LOST;

const CAUSE_OTHER_ACCOUNT =
  "That work belongs to a different account, so prep cannot save it here. " + LOST;

const CAUSE_REFUSED = "The server would not take that work, so prep cannot save it. " + LOST;

const CAUSE_UNKNOWN =
  "prep cannot tell whether that work reached your account. Removing the " +
  "cards may delete it for good.";

const WIPE_FAILED_TITLE = "Could not clear this browser";

const WIPE_FAILED_BODY =
  "prep could not remove the cards from this browser. Nothing was deleted " +
  "from your account.";

function unsyncedLine(pending) {
  if (!pending) return UNSYNCED_UNKNOWN;
  const queued = [];
  if (pending.reviews) {
    queued.push(pending.reviews + (pending.reviews === 1 ? " review" : " reviews"));
  }
  if (pending.cards) {
    queued.push(pending.cards + (pending.cards === 1 ? " new card" : " new cards"));
  }
  const sentences = [];
  if (queued.length) {
    const one = pending.reviews + pending.cards === 1;
    sentences.push(
      queued.join(" and ") + " on this browser " + (one ? "has" : "have") + " not reached your " +
        "account yet."
    );
  }
  if (pending.rejects) {
    // A rejected row is not queued: the server already refused it, so
    // the second dialog is the only place it can ever be named.
    sentences.push(
      pending.rejects +
        (pending.rejects === 1 ? " item" : " items") +
        " could not be saved to your account."
    );
  }
  if (!sentences.length) return UNSYNCED_UNKNOWN;
  return sentences.join(" ");
}

function causeLine(pending, reason) {
  if (!pending) return CAUSE_UNKNOWN;
  if (reason === SIGNED_OUT) return CAUSE_SIGNED_OUT;
  if (reason === OTHER_ACCOUNT) return CAUSE_OTHER_ACCOUNT;
  if (reason === OFFLINE) return CAUSE_OFFLINE;
  if (reason === REFUSED) return CAUSE_REFUSED;
  return CAUSE_UNKNOWN;
}

// What cancel leaves behind, which differs by caller: the sign-out
// path has a session question in play and the other two do not. The
// retry half must not promise a retry that cannot work.
function cancelLine(reason, offerKeep) {
  const lead = offerKeep
    ? "Cancel keeps you signed in and leaves everything on this browser"
    : "Cancel leaves everything on this browser as it is";
  if (reason === OFFLINE) return lead + ", so prep can try again later.";
  if (reason === SIGNED_OUT) return lead + ". Sign in here to save that work.";
  if (reason === OTHER_ACCOUNT) return lead + ". Sign in to that account here to save that work.";
  return lead + ".";
}

// ---- the three entry points ------------------------------------------

// A wipe that did not commit, reported rather than assumed. One exit:
// the caller decides what follows.
function noticeWipeFailed() {
  return choose({
    className: "offline-wipe-failed-dialog",
    title: WIPE_FAILED_TITLE,
    lines: [WIPE_FAILED_BODY],
    choices: [{key: "ok", label: "OK"}],
  });
}

// The second confirmation, and the only path that destroys work the
// server does not have. `offerKeep` adds the exit that answers ONLY
// the data question, for the caller whose other question was whether
// to leave: cancelling a warning about data must not silently cancel
// a sign-out the user already chose. Resolves {decision, wiped}.
async function confirmDestroyUnsynced(pending, reason, {offerKeep = false} = {}) {
  const choices = [];
  if (offerKeep) choices.push({key: "keep", label: SIGN_OUT_KEEP_LABEL, className: "keep"});
  choices.push({key: "destroy", label: "Remove anyway", className: "danger", run: () => wipeNow()});
  choices.push({key: "cancel", label: "Cancel"});
  const answer = await choose({
    className: "offline-unsynced-dialog",
    title: UNSYNCED_TITLE,
    lines: [unsyncedLine(pending), causeLine(pending, reason), cancelLine(reason, offerKeep)],
    choices,
  });
  if (!answer) return {decision: "cancel", wiped: false};
  return {decision: answer.key, wiped: answer.result === true};
}

// Flush, then wipe, asking first if unsynced work survives the flush.
// Resolves true when the caller should proceed with the action it
// asked about (the forget-device POST), which a failed wipe must not
// block: the account is unreachable from here either way, and the
// notice tells the user what is still on the browser.
export async function wipeWithConsent() {
  const result = await flushThenWipe();
  if (result.wiped) return true;
  if (result.reason === STORAGE) {
    await noticeWipeFailed();
    return true;
  }
  const outcome = await confirmDestroyUnsynced(result.pending, result.reason);
  if (outcome.decision !== "destroy") return false;
  if (!outcome.wiped) await noticeWipeFailed();
  return true;
}

// The sign-out choice. Three ways out, two of them buttons that sign
// out, because the consequence differs and a mis-read checkbox
// destroys data. Resolves true when the caller should proceed with
// the sign-out navigation. Every exit past the first button does:
// the user asked to leave, and only the removal is still in question.
export async function confirmSignOut() {
  const answer = await choose({
    className: "offline-signout-dialog",
    title: SIGN_OUT_TITLE,
    lines: [SIGN_OUT_STAYS, SIGN_OUT_REMOVES],
    choices: [
      {key: "keep", label: SIGN_OUT_KEEP_LABEL, className: "keep"},
      {
        key: "remove",
        label: "Sign out and remove them",
        className: "danger",
        // Runs while the session is still alive: after the
        // navigation there is nothing left to flush with.
        run: () => flushThenWipe(),
      },
      {key: "cancel", label: "Cancel"},
    ],
  });
  if (!answer || answer.key === "cancel") return false;
  if (answer.key === "keep") return true;
  const result = answer.result || {wiped: false, pending: null, reason: STORAGE};
  if (result.wiped) return true;
  if (result.reason === STORAGE) {
    await noticeWipeFailed();
    return true;
  }
  const outcome = await confirmDestroyUnsynced(result.pending, result.reason, {offerKeep: true});
  if (outcome.decision === "cancel") return false;
  if (outcome.decision === "destroy" && !outcome.wiped) await noticeWipeFailed();
  return true;
}

// The landing page's removal action. Resolves true when the device
// was cleared, and only then: the caller reloads onto the splash.
export async function confirmRemoveFromDevice() {
  const answer = await choose({
    className: "offline-remove-dialog",
    title: REMOVE_TITLE,
    lines: [REMOVE_CLEARS],
    choices: [
      {key: "remove", label: "Remove them", className: "danger", run: () => flushThenWipe()},
      {key: "cancel", label: "Cancel"},
    ],
  });
  if (!answer || answer.key !== "remove") return false;
  const result = answer.result || {wiped: false, pending: null, reason: STORAGE};
  if (result.wiped) return true;
  if (result.reason === STORAGE) {
    await noticeWipeFailed();
    return false;
  }
  const outcome = await confirmDestroyUnsynced(result.pending, result.reason);
  if (outcome.decision !== "destroy") return false;
  if (!outcome.wiped) {
    await noticeWipeFailed();
    return false;
  }
  return true;
}
