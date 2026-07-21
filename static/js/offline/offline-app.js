// offline-app.js: bootstrap for the /offline shell. Client-rendered
// on purpose: the shell is served from the SW cache with no network
// and no server-resolved user, so everything here reads IndexedDB
// (docs/OFFLINE.md section 3).
//
// M3 scope: offline STUDY. The overview (owner line, per-deck counts,
// due list) gains a Study flow: one card per screen, deterministic
// grading via grader.js where the card type allows it (mcq, multi,
// regex short-answer), reveal + self-verdict everywhere else. Every
// verdict writes a queued review to the outbox (drained by sync.js
// when back online) plus the local ladder overlay via scheduler.js so
// studied cards re-surface on the offline schedule (docs/OFFLINE.md
// section 5). Reconnect shows a banner, flushes, and toasts the
// result. Authoring arrives in M4; confirm-wipe + needs-attention in
// M5.
//
// Plain DOM building, no framework, no innerHTML for data (card
// prompts, choices, and answers are user content; textContent only).

import {getAll, metaGet, put, uuid, withLock} from "./store.js";
import {flushOutbox, refreshSnapshot, showToast} from "./sync.js";
import * as grader from "./grader.js";
import * as scheduler from "./scheduler.js";

// The deploy's root path, derived from this module's own URL (same
// trick as sync.js: the module is served under <root>/static/js/...).
const ROOT_PATH = new URL(import.meta.url).pathname.replace(/\/static\/js\/.*$/, "");

// ---- icons -----------------------------------------------------------
// Phosphor Light path data (copied from static/icons/*.svg). Inlined
// because the online app's icon() helper is a server-side Jinja
// global and the raw icon files are not in the SW precache. Trusted
// static markup, built via createElementNS, never innerHTML.

const SVG_NS = "http://www.w3.org/2000/svg";
const ICON_PATHS = {
  check:
    "M228.24,76.24l-128,128a6,6,0,0,1-8.48,0l-56-56a6,6,0,0,1,8.48-8.48L96,191.51,219.76,67.76a6,6,0,0,1,8.48,8.48Z",
  x:
    "M204.24,195.76a6,6,0,1,1-8.48,8.48L128,136.49,60.24,204.24a6,6,0,0,1-8.48-8.48L119.51,128,51.76,60.24a6,6,0,0,1,8.48-8.48L128,119.51l67.76-67.75a6,6,0,0,1,8.48,8.48L136.49,128Z",
  circle:
    "M128,26A102,102,0,1,0,230,128,102.12,102.12,0,0,0,128,26Zm0,192a90,90,0,1,1,90-90A90.1,90.1,0,0,1,128,218Z",
  dot: "M138,128a10,10,0,1,1-10-10A10,10,0,0,1,138,128Z",
  "arrow-left":
    "M222,128a6,6,0,0,1-6,6H54.49l61.75,61.76a6,6,0,1,1-8.48,8.48l-72-72a6,6,0,0,1,0-8.48l72-72a6,6,0,0,1,8.48,8.48L54.49,122H216A6,6,0,0,1,222,128Z",
};

function icon(name, className = "icon") {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 256 256");
  svg.setAttribute("fill", "currentColor");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", className);
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", ICON_PATHS[name] || "");
  svg.appendChild(path);
  return svg;
}

// ---- tiny DOM helpers ------------------------------------------------

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// The prelude pattern shared with the online settings pages: eyebrow,
// display headline (with an italic beat), lede.
function prelude(eyebrowText, headStart, headEm, ledeText) {
  const section = el("section", "prelude");
  section.appendChild(el("p", "eyebrow", eyebrowText));
  const h1 = el("h1", "display", headStart + " ");
  h1.appendChild(el("em", null, headEm));
  h1.appendChild(document.createTextNode("."));
  section.appendChild(h1);
  section.appendChild(el("p", "lede", ledeText));
  return section;
}

function sectionEyebrow(label, aside) {
  const p = el("p", "section-eyebrow");
  p.appendChild(el("span", null, label));
  p.appendChild(el("span", "rule"));
  if (aside) p.appendChild(el("span", "eyebrow-aside", aside));
  return p;
}

// ---- due-time math ---------------------------------------------------

// A card's effective due time offline: the local ladder overlay when
// set, else the snapshot's server-computed next_due.
function effectiveDue(card) {
  return card.local_next_due || card.next_due || null;
}

// Due now = effective due parses and is in the past (via the ladder's
// due()). A null due (a card the server considers due immediately)
// counts as due; an unparseable timestamp does not (junk must not
// flood the queue).
function isDueNow(card, now) {
  // scheduler.due owns the whole contract, including the fail-open
  // rule: a missing or unparseable next_due counts as DUE. Filtering
  // junk out here would silently vanish the card from the queue and
  // deck counts forever; surfacing it costs one early review.
  return scheduler.due(now, effectiveDue(card));
}

function dueTime(card) {
  const dueAt = effectiveDue(card);
  if (dueAt === null) return 0;
  const t = Date.parse(dueAt);
  return Number.isFinite(t) ? t : 0;
}

// Format a minute count the way the online result page does:
// min / hr / day / week / month.
function humanMinutes(m) {
  if (m < 60) return m + " min";
  if (m < 24 * 60) return Math.floor(m / 60) + " hr";
  const days = Math.floor(m / (24 * 60));
  if (days === 1) return "1 day";
  if (days < 7) return days + " days";
  if (days < 30) {
    const weeks = Math.floor(days / 7);
    return weeks + " week" + (weeks > 1 ? "s" : "");
  }
  const months = Math.floor(days / 30);
  return months + " month" + (months > 1 ? "s" : "");
}

// ---- state -----------------------------------------------------------

const state = {
  owner: null,
  decks: [],
  cards: [],
  outboxCount: 0,
};

let root = null;
let viewName = "loading";

async function reloadLocal() {
  const [owner, decks, cards, outbox] = await Promise.all([
    metaGet("owner"),
    getAll("decks"),
    getAll("cards"),
    getAll("outbox_reviews"),
  ]);
  state.owner = owner;
  state.decks = decks;
  state.cards = cards;
  state.outboxCount = outbox.length;
}

function show(node, name) {
  root.replaceChildren(node);
  viewName = name;
  window.scrollTo(0, 0);
}

// ---- the study ledger ------------------------------------------------

// Every verdict writes two things (docs/OFFLINE.md section 2): the
// queued review for sync, and the local ladder overlay so the card
// re-surfaces offline. The transition is computed first (pure), then
// the outbox row, then the overlay; if the overlay write loses a
// race with a crash the card just comes back early, while the review
// itself is already safely queued.
async function recordVerdict(card, verdict, userAnswer, gradedBy) {
  // reviewed_at MUST be new Date().toISOString(): flushOutbox orders
  // rows by LEXICOGRAPHIC reviewed_at comparison, which is only
  // chronological when every timestamp is uniform-offset UTC ISO-8601.
  const reviewedAt = new Date().toISOString();
  const seedStep = card.local_step ?? card.step ?? 0;
  const t = scheduler.transition(seedStep, verdict);
  // Locked against sync.js's snapshot-refresh overlay merge: a
  // refresh in flight between our outbox write and overlay write
  // would wipe the overlay this tap creates (its pending-ids
  // snapshot predates us). The lock makes tap and merge take turns.
  const updated = await withLock(async () => {
    await put("outbox_reviews", {
      client_id: uuid(),
      question_id: card.question_id,
      verdict,
      user_answer: userAnswer,
      graded_by: gradedBy,
      reviewed_at: reviewedAt,
    });
    const row = {
      ...card,
      local_step: t.step,
      // nextDueIso emits the same uniform-offset UTC shape as
      // toISOString, keeping every timestamp we write on the
      // lexicographic-ordering contract by construction.
      local_next_due: scheduler.nextDueIso(Date.now(), t.next_due_minutes),
    };
    await put("cards", row);
    return row;
  });
  const i = state.cards.findIndex((c) => c.question_id === card.question_id);
  if (i !== -1) state.cards[i] = updated;
  state.outboxCount += 1;
  return t;
}

// ---- shared render pieces --------------------------------------------

// Pending affordance for the study buttons: is-loading (spinner via
// buttons.css) + a re-entrancy guard, no disabled attribute so the
// button's box never restyles mid-tap (no layout shift).
let actionInFlight = false;

async function runPending(button, fn) {
  // One guard for the whole view, not per-button: Submit then "I
  // don't know" (or right then wrong) tapped in the same beat would
  // otherwise BOTH record, writing two outbox rows with distinct
  // client ids that server idempotency cannot dedupe.
  if (actionInFlight) return;
  actionInFlight = true;
  button.classList.add("is-loading");
  try {
    await fn();
  } catch (e) {
    console.warn("offline study action failed:", e);
    showToast("Could not save that. Try again.");
  } finally {
    actionInFlight = false;
    button.classList.remove("is-loading");
  }
}

function studyNav(card) {
  const nav = el("nav", "study-nav");
  const back = el("button", "offline-linkbtn back");
  back.type = "button";
  back.appendChild(icon("arrow-left", "icon icon-inline"));
  back.appendChild(document.createTextNode(" Pause"));
  back.addEventListener("click", () => renderOverview());
  nav.appendChild(back);
  nav.appendChild(el("span", "card-id", "№ " + card.question_id));
  return nav;
}

function parseJsonArray(text) {
  try {
    const parsed = JSON.parse(text);
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch (e) {
    return [];
  }
}

function answerBlock(card, text, isModel) {
  if (card.type === "code") {
    return el("pre", "reproduction" + (isModel ? " reproduction-model" : ""), text);
  }
  return el("blockquote", "prose-answer" + (isModel ? " prose-answer-model" : ""), text);
}

// The post-answer choice grid, same visual states as result.html:
// correct-picked / wrong-picked / correct-missed / idle.
function choiceGrid(card, picked, correct) {
  const pickedSet = new Set(picked);
  const correctSet = new Set(correct);
  const list = el("ul", "answer-grid");
  list.setAttribute("role", "list");
  const options = card.choices && card.choices.length ? card.choices : correct;
  for (const choice of options) {
    const wasPicked = pickedSet.has(choice);
    const isCorrect = correctSet.has(choice);
    const cls =
      wasPicked && isCorrect
        ? "correct-picked"
        : wasPicked
          ? "wrong-picked"
          : isCorrect
            ? "correct-missed"
            : "idle";
    const row = el("li", "answer-row state-" + cls);
    const marker = el("span", "answer-marker");
    const markerIcon =
      cls === "correct-picked"
        ? "check"
        : cls === "wrong-picked"
          ? "x"
          : cls === "correct-missed"
            ? "circle"
            : "dot";
    marker.appendChild(icon(markerIcon));
    row.appendChild(marker);
    row.appendChild(el("span", "answer-text", choice));
    const tags = el("span", "answer-tags");
    if (wasPicked) tags.appendChild(el("span", "tag tag-pick", "your pick"));
    if (isCorrect) tags.appendChild(el("span", "tag tag-correct", "correct"));
    row.appendChild(tags);
    list.appendChild(row);
  }
  return list;
}

// The answer-compare sections shared by the reveal and verdict views.
// mcq/multi render the choice grid; everything else renders the
// user's text (skipped on idk, like the online result page) then the
// model answer, then the rubric when present.
function compareSections(card, userAnswer, opts) {
  const sections = [];
  if (card.type === "mcq" || card.type === "multi") {
    const section = el("section", "result-section");
    section.appendChild(
      sectionEyebrow("Choices", card.type === "multi" ? "pick all that apply" : null)
    );
    const correct =
      card.type === "multi" ? parseJsonArray(card.answer || "") : [card.answer].filter(Boolean);
    const picked = opts.idk
      ? []
      : card.type === "multi"
        ? parseJsonArray(userAnswer || "")
        : userAnswer
          ? [userAnswer]
          : [];
    section.appendChild(choiceGrid(card, picked, correct));
    sections.push(section);
    return sections;
  }
  if (!opts.idk) {
    const mine = el("section", "result-section");
    mine.appendChild(sectionEyebrow(opts.userLabel));
    mine.appendChild(answerBlock(card, userAnswer || "(blank)", false));
    sections.push(mine);
  }
  const model = el("section", "result-section");
  model.appendChild(sectionEyebrow(opts.modelLabel));
  model.appendChild(answerBlock(card, card.answer || "", true));
  sections.push(model);
  if (card.rubric) {
    const rubric = el("section", "result-section");
    rubric.appendChild(sectionEyebrow("Rubric"));
    rubric.appendChild(el("pre", "reproduction reproduction-rubric", card.rubric));
    sections.push(rubric);
  }
  return sections;
}

// ---- views -----------------------------------------------------------

function renderEmpty() {
  show(
    prelude(
      "Offline study",
      "Nothing cached",
      "yet",
      "Open prep while online first. Your decks and due cards are " +
        "saved to this device automatically, and this screen works " +
        "without a connection after that."
    ),
    "empty"
  );
}

function renderOverview() {
  const now = Date.now();
  const dueCards = state.cards
    .filter((card) => isDueNow(card, now))
    .sort((a, b) => dueTime(a) - dueTime(b));

  const frag = document.createDocumentFragment();

  // ---- prelude + owner line -----------------------------------------
  const who = state.owner.display_name || "you";
  const lede =
    "Studying as " + who + ". " +
    (dueCards.length
      ? dueCards.length + (dueCards.length === 1 ? " card is" : " cards are") +
        " due right now."
      : "Nothing is due right now.");
  frag.appendChild(prelude("Offline study", "Your cards,", "offline", lede));

  // ---- study CTA ----------------------------------------------------
  if (dueCards.length) {
    const actions = el("div", "study-actions offline-study-cta");
    const studyBtn = el("button", "btn btn-primary", "Study");
    studyBtn.type = "button";
    studyBtn.addEventListener("click", () => startStudy());
    actions.appendChild(studyBtn);
    frag.appendChild(actions);
  }

  // ---- per-deck counts ----------------------------------------------
  const deckSection = el("section", "offline-decks");
  deckSection.appendChild(sectionEyebrow("Decks"));
  if (state.decks.length === 0) {
    deckSection.appendChild(el("p", "muted", "No decks in the snapshot."));
  } else {
    const byDeck = new Map();
    for (const card of state.cards) {
      const entry = byDeck.get(card.deck_id) || {total: 0, due: 0};
      entry.total += 1;
      if (isDueNow(card, now)) entry.due += 1;
      byDeck.set(card.deck_id, entry);
    }
    const list = el("ul", "offline-deck-list");
    for (const deck of state.decks) {
      const counts = byDeck.get(deck.id) || {total: 0, due: 0};
      const item = el("li", "offline-deck");
      item.appendChild(el("span", "offline-deck-name", deck.display_name || deck.name));
      item.appendChild(
        el(
          "span",
          "offline-deck-counts muted",
          counts.due + " due · " + counts.total +
            (counts.total === 1 ? " card" : " cards")
        )
      );
      list.appendChild(item);
    }
    deckSection.appendChild(list);
  }
  frag.appendChild(deckSection);

  // ---- due-now list (glanceable prompts) ----------------------------
  const dueSection = el("section", "offline-due");
  dueSection.appendChild(sectionEyebrow("Due now"));
  if (dueCards.length === 0) {
    dueSection.appendChild(
      el("p", "muted", "Nothing due. Check back later, or come back online to sync.")
    );
  } else {
    const list = el("ul", "offline-due-list");
    for (const card of dueCards) {
      list.appendChild(el("li", "offline-due-card", card.prompt || ""));
    }
    dueSection.appendChild(list);
  }
  frag.appendChild(dueSection);

  // ---- footer lines -------------------------------------------------
  if (state.outboxCount) {
    frag.appendChild(
      el(
        "p",
        "muted offline-outbox-note",
        state.outboxCount +
          (state.outboxCount === 1 ? " review" : " reviews") +
          " waiting to sync."
      )
    );
  }
  if (state.owner.snapshot_at) {
    const stamp = Date.parse(state.owner.snapshot_at);
    const label = Number.isFinite(stamp)
      ? new Date(stamp).toLocaleString()
      : state.owner.snapshot_at;
    frag.appendChild(el("p", "muted offline-snapshot-stamp", "Snapshot from " + label + "."));
  }

  show(frag, "overview");
}

// The queue is recomputed on every advance (docs/OFFLINE.md section
// 2): oldest effective due first, so a card answered wrong (+10m)
// naturally returns later in a long sitting.
function nextDueCard() {
  const now = Date.now();
  const dueCards = state.cards.filter((card) => isDueNow(card, now));
  if (!dueCards.length) return null;
  dueCards.sort((a, b) => dueTime(a) - dueTime(b));
  return dueCards[0];
}

function startStudy() {
  const card = nextDueCard();
  if (card) renderStudyCard(card);
  else renderCaughtUp();
}

function renderStudyCard(card) {
  const frag = document.createDocumentFragment();
  frag.appendChild(studyNav(card));

  const article = el("article", "study-card");
  const head = el("header", "study-head");
  head.appendChild(el("span", "tag tag-type tag-" + (card.type || "short"), card.type || "short"));
  article.appendChild(head);
  article.appendChild(el("div", "study-prompt", card.prompt || ""));

  const form = document.createElement("form");
  form.className = "study-form";

  let collect;
  if (card.type === "mcq" || card.type === "multi") {
    const multi = card.type === "multi";
    const fieldset = el("fieldset", "choices" + (multi ? " choices-multi" : ""));
    fieldset.appendChild(
      el("legend", "visually-hidden", multi ? "Pick all that apply" : "Choose one")
    );
    for (const choice of card.choices || []) {
      const label = el("label", "choice");
      const input = document.createElement("input");
      input.type = multi ? "checkbox" : "radio";
      input.name = "choice";
      input.value = choice;
      if (!multi) input.required = true;
      label.appendChild(input);
      label.appendChild(el("span", "choice-marker"));
      label.appendChild(el("span", "choice-text", choice));
      fieldset.appendChild(label);
    }
    form.appendChild(fieldset);
    collect = () => {
      const picked = Array.from(form.querySelectorAll("input:checked"), (i) => i.value);
      // Mirror the online wire form (prep/study/routes.py
      // _read_user_answer): mcq stores the choice string, multi a
      // sorted JSON array string.
      return multi ? JSON.stringify(picked.sort()) : picked[0] || "";
    };
  } else {
    const wrap = el("label", "freetext");
    wrap.appendChild(
      el("span", "freetext-label", card.type === "code" ? "Your code" : "Your answer")
    );
    const ta = document.createElement("textarea");
    ta.rows = card.type === "code" ? 10 : 6;
    if (card.type === "code") {
      ta.className = "code-area";
      ta.spellcheck = false;
      ta.placeholder = "Write it out. Pseudocode is fine if the idea is clear.";
      if (card.skeleton) ta.value = card.skeleton;
    } else {
      ta.placeholder = "A sentence or two.";
    }
    wrap.appendChild(ta);
    form.appendChild(wrap);
    collect = () => ta.value;
  }

  const actions = el("div", "study-actions");
  const submitBtn = el("button", "btn btn-primary", "Submit");
  submitBtn.type = "submit";
  const idkBtn = el("button", "btn btn-quiet", "I don't know");
  idkBtn.type = "button";
  actions.appendChild(submitBtn);
  actions.appendChild(idkBtn);
  form.appendChild(actions);

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    runPending(submitBtn, async () => {
      const answer = collect();
      let graded = null;
      try {
        graded = grader.grade(card, answer);
      } catch (e) {
        graded = null; // an ungradeable card falls through to self-verdict
      }
      if (graded && graded.verdict) {
        const t = await recordVerdict(card, graded.verdict, answer, "auto");
        renderVerdict(card, graded.verdict, answer, {minutes: t.next_due_minutes, idk: false});
      } else {
        renderReveal(card, answer);
      }
    });
  });

  // "I don't know": wrong verdict with an empty answer, same as the
  // online idk path. Deterministic, so graded_by stays "auto".
  idkBtn.addEventListener("click", () => {
    runPending(idkBtn, async () => {
      const t = await recordVerdict(card, "wrong", "", "auto");
      renderVerdict(card, "wrong", "", {minutes: t.next_due_minutes, idk: true});
    });
  });

  article.appendChild(form);
  frag.appendChild(article);
  show(frag, "study");
}

// Reveal + self-verdict: the offline analogue of self_grade.html, for
// card types with no deterministic grader (code, short without a
// usable regex).
function renderReveal(card, answer) {
  const frag = document.createDocumentFragment();
  frag.appendChild(studyNav(card));

  const article = el("article", "study-card");
  article.appendChild(sectionEyebrow("Self-grade"));
  article.appendChild(
    el(
      "p",
      "muted offline-selfgrade-blurb",
      "No deterministic grader applies offline, so you're the judge. " +
        "Compare what you wrote against the canonical answer and pick " +
        "honestly. The scheduler works either way."
    )
  );
  article.appendChild(sectionEyebrow("The question"));
  article.appendChild(el("div", "study-prompt", card.prompt || ""));
  const sections = compareSections(card, answer, {
    idk: false,
    userLabel: "Your answer",
    modelLabel: "Canonical answer",
  });
  for (const section of sections) article.appendChild(section);

  const actions = el("div", "study-actions");
  const rightBtn = el("button", "btn btn-primary", "I got it right");
  rightBtn.type = "button";
  const wrongBtn = el("button", "btn btn-quiet", "I got it wrong");
  wrongBtn.type = "button";
  const decide = (verdict, button) =>
    runPending(button, async () => {
      const t = await recordVerdict(card, verdict, answer, "self");
      renderVerdict(card, verdict, answer, {minutes: t.next_due_minutes, idk: false});
    });
  rightBtn.addEventListener("click", () => decide("right", rightBtn));
  wrongBtn.addEventListener("click", () => decide("wrong", wrongBtn));
  actions.appendChild(rightBtn);
  actions.appendChild(wrongBtn);
  article.appendChild(actions);

  frag.appendChild(article);
  show(frag, "reveal");
}

function renderVerdict(card, verdict, userAnswer, opts) {
  const frag = document.createDocumentFragment();
  frag.appendChild(studyNav(card));

  const right = verdict === "right";
  const block = el("section", "verdict-block");
  const mark = el("span", "verdict-mark " + (right ? "verdict-mark-right" : "verdict-mark-wrong"));
  mark.appendChild(icon(right ? "check" : "x"));
  block.appendChild(mark);
  block.appendChild(el("h1", "verdict-headline", right ? "Right." : "Not yet."));
  const sub = el("p", "verdict-sub", "Next review in ");
  sub.appendChild(el("strong", null, humanMinutes(opts.minutes)));
  sub.appendChild(document.createTextNode(" · offline schedule"));
  block.appendChild(sub);
  frag.appendChild(block);

  const question = el("section", "result-section");
  question.appendChild(sectionEyebrow("The question"));
  question.appendChild(el("div", "study-prompt", card.prompt || ""));
  frag.appendChild(question);

  const sections = compareSections(card, userAnswer, {
    idk: opts.idk,
    userLabel: "What you wrote",
    modelLabel: "Model answer",
  });
  for (const section of sections) frag.appendChild(section);

  const actions = el("div", "study-actions next-actions");
  const nextBtn = el("button", "btn btn-primary", "Next card");
  nextBtn.type = "button";
  nextBtn.addEventListener("click", () => startStudy());
  const pauseBtn = el("button", "btn btn-quiet", "Pause");
  pauseBtn.type = "button";
  pauseBtn.addEventListener("click", () => renderOverview());
  actions.appendChild(nextBtn);
  actions.appendChild(pauseBtn);
  frag.appendChild(actions);

  show(frag, "verdict");
}

function nextDueInMinutes() {
  const now = Date.now();
  let best = null;
  for (const card of state.cards) {
    const t = Date.parse(effectiveDue(card) || "");
    if (!Number.isFinite(t) || t <= now) continue;
    if (best === null || t < best) best = t;
  }
  if (best === null) return null;
  return Math.max(1, Math.ceil((best - now) / 60000));
}

function renderCaughtUp() {
  const frag = document.createDocumentFragment();
  const section = el("section", "empty-state");
  const h = el("h2", "empty-headline", "All caught up ");
  h.appendChild(el("em", null, "offline"));
  h.appendChild(document.createTextNode("."));
  section.appendChild(h);
  const minutes = nextDueInMinutes();
  section.appendChild(
    el(
      "p",
      "empty-sub",
      minutes === null
        ? "Nothing else is scheduled on this device."
        : "The next card comes due in " + humanMinutes(minutes) + "."
    )
  );
  const back = el("button", "btn btn-quiet", "Back to overview");
  back.type = "button";
  back.addEventListener("click", () => renderOverview());
  section.appendChild(back);
  frag.appendChild(section);
  show(frag, "caughtup");
}

// ---- reconnect + sync ------------------------------------------------

let bannerNode = null;
let syncing = false;

function ensureBanner() {
  if (!bannerNode) {
    bannerNode = el("div", "offline-banner");
    bannerNode.setAttribute("role", "status");
    bannerNode.hidden = true;
    document.body.appendChild(bannerNode);
  }
  return bannerNode;
}

function showBanner(text, link) {
  const banner = ensureBanner();
  banner.replaceChildren(document.createTextNode(text));
  if (link) {
    banner.appendChild(document.createTextNode(" "));
    const a = el("a", null, link.label);
    a.href = link.href;
    banner.appendChild(a);
  }
  banner.hidden = false;
}

function hideBanner() {
  if (bannerNode) bannerNode.hidden = true;
}

// navigator.onLine alone lies on captive/one-bar networks; confirm
// with a lightweight probe against the un-auth-gated liveness route.
async function probeOnline() {
  if (!navigator.onLine) return false;
  try {
    const response = await fetch(ROOT_PATH + "/healthz", {cache: "no-store"});
    return response.ok;
  } catch (e) {
    return false;
  }
}

// When connectivity returns with reviews queued: banner, flush,
// forced snapshot refresh (which preserves overlays for anything
// still queued), toast the result. If the flush cannot run from the
// shell (dead session cookies, owner mismatch), hand off to the
// online app, which can mint fresh credentials via its reauth flow.
async function syncOnReconnect() {
  if (syncing) return;
  syncing = true;
  try {
    const queued = await getAll("outbox_reviews");
    if (!queued.length) return;
    if (!(await probeOnline())) return;
    showBanner("Back online - syncing…");
    const result = await flushOutbox();
    const moved = (result.flushed || 0) + (result.rejected || 0);
    if (result.disabled || result.status || moved === 0) {
      showBanner("Back online.", {href: ROOT_PATH + "/", label: "Open prep to finish syncing."});
      return;
    }
    await refreshSnapshot({force: true});
    await reloadLocal();
    hideBanner();
    const bits = [];
    if (result.flushed) {
      bits.push(result.flushed === 1 ? "1 review synced" : result.flushed + " reviews synced");
    }
    if (result.rejected) bits.push(result.rejected + " rejected");
    showToast(bits.join(", "));
    if (viewName === "overview") renderOverview();
    else if (viewName === "caughtup") renderCaughtUp();
  } catch (e) {
    console.warn("offline reconnect sync failed:", e);
    hideBanner();
  } finally {
    syncing = false;
  }
}

// ---- boot ------------------------------------------------------------

async function boot() {
  root = document.getElementById("offline-root") || document.body;
  try {
    await reloadLocal();
  } catch (e) {
    // IndexedDB unavailable (private-mode quirks, storage wiped mid
    // read). Degrade to the honest empty state rather than a blank page.
    console.warn("offline app failed to read local data:", e);
    renderEmpty();
    return;
  }
  if (!state.owner) {
    renderEmpty();
    return;
  }
  renderOverview();
  window.addEventListener("online", () => {
    syncOnReconnect();
  });
  window.addEventListener("offline", hideBanner);
  // The page may have been opened online with a queued outbox
  // (preflight, or study-then-reconnect without a reload).
  syncOnReconnect();
}

boot();
