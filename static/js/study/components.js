// components.js: the study-loop views as standalone functions. Each
// takes plain card data plus callbacks and returns detached DOM; no
// IndexedDB access, no sync knowledge, no navigation. The two hosts
// (the offline shell and the signed-in study page) own state, mount
// the returned nodes, and decide what renders next.
//
// Copy that only one surface can truthfully say (the offline blurbs,
// the AI feedback block, the chat handoff) is a per-call option with a
// default, never a branch on which host is calling.
//
// Plain DOM building, no framework. Card prompts, choices, and
// answers are user content: prompts render via the escape-first
// markdown renderer (whose output is safe for innerHTML by
// construction), everything else is textContent only.

import {el, icon, sectionEyebrow, humanMinutes} from "./dom.js";
import {markdownHTML} from "./markdown.js";

// ---- pending affordance ------------------------------------------------

// Pending affordance for the study buttons: is-loading (spinner via
// buttons.css) + a re-entrancy guard, no disabled attribute so the
// button's box never restyles mid-tap (no layout shift).
//
// One guard for the whole page, not per-button: Submit then "I
// don't know" (or right then wrong) tapped in the same beat would
// otherwise BOTH record, writing two outbox rows with distinct
// client ids that server idempotency cannot dedupe.
let actionInFlight = false;

// Failure handling is host-owned (the offline shell toasts; another
// host may not have toasts at all).
let actionErrorHandler = (e) => {
  console.warn("study action failed:", e);
};

export function setActionErrorHandler(fn) {
  actionErrorHandler = fn;
}

export async function runPending(button, fn) {
  if (actionInFlight) return;
  actionInFlight = true;
  button.classList.add("is-loading");
  try {
    await fn();
  } catch (e) {
    actionErrorHandler(e);
  } finally {
    actionInFlight = false;
    button.classList.remove("is-loading");
  }
}

// ---- shared pieces -----------------------------------------------------

export function studyNav(card, onPause) {
  const nav = el("nav", "study-nav");
  const back = el("button", "offline-linkbtn back");
  back.type = "button";
  back.appendChild(icon("arrow-left", "icon icon-inline"));
  back.appendChild(document.createTextNode(" Pause"));
  back.addEventListener("click", () => onPause());
  nav.appendChild(back);
  nav.appendChild(
    el("span", "card-id", card.question_id ? "№ " + card.question_id : "new card")
  );
  return nav;
}

// The prompt block, markdown-rendered: same DOM shape as the online
// templates' `study-prompt prose` div.
// A renderer throw (e.g. stack overflow on pathological nesting)
// falls back to plain text so the card stays studyable.
function promptNode(card) {
  const div = el("div", "study-prompt prose");
  try {
    div.innerHTML = markdownHTML(card.prompt || "");
  } catch (e) {
    div.textContent = card.prompt || "";
  }
  return div;
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
    // Multi-select scores partially, so the counts say more than the
    // grid colours alone.
    if (card.type === "multi") {
      const hit = picked.filter((p) => correct.includes(p)).length;
      const wrong = picked.filter((p) => !correct.includes(p)).length;
      const missed = correct.filter((c) => !picked.includes(c)).length;
      section.appendChild(
        el(
          "p",
          "answer-tally",
          `${hit} correct picked · ${wrong} wrong included · ${missed} correct missed`
        )
      );
    }
    sections.push(section);
    sections.push(...rubricSections(card));
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
  sections.push(...rubricSections(card));
  return sections;
}

// The grading rubric, collapsed. Every card type that carries one
// shows it: the choice grid returns early, so it needs its own call.
function rubricSections(card) {
  if (!card.rubric) return [];
  const section = el("section", "result-section");
  const details = el("details", "rubric-details");
  const summary = document.createElement("summary");
  summary.textContent = "Show the grading rubric";
  details.appendChild(summary);
  details.appendChild(el("pre", "reproduction reproduction-rubric", card.rubric));
  section.appendChild(details);
  return [section];
}

// ---- views -------------------------------------------------------------

// The answer-entry card. Callbacks: onAnswer(answerString) grades and
// records (or decides the reveal flow is next), onIdk() records a
// wrong verdict, onPause() leaves the loop, onDraft(text) sees every
// keystroke (a host with server-side autosave debounces it).
//
// `draft` seeds the text area when the host has a saved one; a code
// card falls back to its skeleton.
export function studyCardView(card, {onAnswer, onIdk, onPause, onDraft, draft}) {
  const frag = document.createDocumentFragment();
  frag.appendChild(studyNav(card, onPause));

  const article = el("article", "study-card");
  const head = el("header", "study-head");
  head.appendChild(el("span", "tag tag-type tag-" + (card.type || "short"), card.type || "short"));
  if (card.topic) head.appendChild(el("span", "tag tag-topic", card.topic));
  article.appendChild(head);
  article.appendChild(promptNode(card));

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
      // The online wire form: mcq stores the choice string, multi a
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
      ta.value = typeof draft === "string" ? draft : card.skeleton || "";
      if (card.language) ta.dataset.language = card.language;
      if (card.skeleton) ta.dataset.skeleton = card.skeleton;
    } else {
      ta.placeholder = "A sentence or two.";
      if (typeof draft === "string") ta.value = draft;
    }
    if (onDraft) ta.addEventListener("input", () => onDraft(ta.value));
    wrap.appendChild(ta);
    form.appendChild(wrap);
    collect = () => ta.value;
    if (card.type === "code") upgradeToCodeEditor(ta, wrap, card);
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
    runPending(submitBtn, () => onAnswer(collect()));
  });

  idkBtn.addEventListener("click", () => {
    runPending(idkBtn, () => onIdk());
  });

  article.appendChild(form);
  frag.appendChild(article);
  return frag;
}

// Code cards get a real editor when the bundle is reachable. Async and
// fire-and-forget on purpose: the textarea is already interactive, so
// the upgrade never blocks the card from rendering, and a failed load
// (offline, cache miss) simply leaves the plain textarea in place.
function upgradeToCodeEditor(textarea, wrap, card) {
  const rootPath = new URL(import.meta.url).pathname.replace(/\/static\/js\/.*$/, "");
  const inputMode = document.body.dataset.editorMode || "vanilla";
  import("./code-editor.js")
    .then(({mountCodeEditor, codeToolbar}) =>
      mountCodeEditor(textarea, {
        rootPath,
        language: card.language || "",
        skeleton: card.skeleton || "",
        inputMode,
      }).then((handle) => {
        if (!handle || !textarea.isConnected) return;
        wrap.appendChild(codeToolbar(handle, {inputMode, hasSkeleton: Boolean(card.skeleton)}));
      })
    )
    .catch(() => {});
}

const DEFAULT_REVEAL_BLURB =
  "No deterministic grader applies offline, so you're the judge. " +
  "Compare what you wrote against the canonical answer and pick " +
  "honestly. The scheduler works either way.";

// Reveal + self-verdict, for card types with no grader available
// (code, short without a usable regex; online, no configured AI
// grader). onVerdict("right"|"wrong") records the choice. `blurb`
// says WHY the user is judging, which differs per host.
export function revealView(card, answer, {onVerdict, onPause, blurb}) {
  const frag = document.createDocumentFragment();
  frag.appendChild(studyNav(card, onPause));

  const article = el("article", "study-card");
  article.appendChild(sectionEyebrow("Self-grade"));
  article.appendChild(el("p", "muted offline-selfgrade-blurb", blurb || DEFAULT_REVEAL_BLURB));
  article.appendChild(sectionEyebrow("The question"));
  article.appendChild(promptNode(card));
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
  const decide = (verdict, button) => runPending(button, () => onVerdict(verdict));
  rightBtn.addEventListener("click", () => decide("right", rightBtn));
  wrongBtn.addEventListener("click", () => decide("wrong", wrongBtn));
  actions.appendChild(rightBtn);
  actions.appendChild(wrongBtn);
  article.appendChild(actions);

  frag.appendChild(article);
  return frag;
}

// Grading-in-flight: the screen between a submitted answer and a
// verdict computed somewhere else. Same chrome as the server-rendered
// grading page (.grading-panel), so the two surfaces read alike.
//
// `pending` is the source's Pending ({settled, cancel, ...}). The
// caller awaits `pending.settled` and renders the verdict it resolves
// to; this view only shows the wait. Nothing in it swaps text or
// resizes, so the panel holds one box for as long as the grade takes.
export function pendingView(card, pending, {onPause}) {
  const frag = document.createDocumentFragment();
  // A session resumed mid-grade knows the workflow but not the card
  // it belongs to, so the nav takes whatever is known.
  frag.appendChild(
    studyNav(card || {}, () => {
      // The workflow is durable and keeps running; only the client's
      // polling loop stops, so a resumed session re-enters this view.
      if (pending && pending.cancel) pending.cancel();
      onPause();
    })
  );

  const panel = el("section", "grading-panel");
  panel.appendChild(sectionEyebrow("Grading"));
  const headline = el("h1", "display gen-headline");
  headline.appendChild(el("em", null, "Reading your answer."));
  panel.appendChild(headline);

  const status = el("p", "gen-status");
  status.setAttribute("role", "status");
  status.appendChild(el("span", "status-dot status-grading"));
  status.appendChild(el("span", null, "Grading"));
  const spinner = el("span", "grading-spinner");
  spinner.setAttribute("aria-hidden", "true");
  for (const n of ["d1", "d2", "d3"]) spinner.appendChild(el("span", "dot " + n));
  status.appendChild(spinner);
  panel.appendChild(status);

  const hint = el("p", "gen-hint");
  hint.appendChild(
    el(
      "em",
      null,
      "Usually 5 to 20 seconds. The grade runs on the server, so it " +
        "finishes even if you leave this screen."
    )
  );
  panel.appendChild(hint);

  // The worker reports trouble it can keep running through (a busy
  // shared grader pointing at BYOK, for one). Showing it is the only
  // way the user learns why the wait is long.
  const note = pending && pending.error;
  if (note) panel.appendChild(el("p", "muted gen-error", note));

  frag.appendChild(panel);
  return frag;
}

// The post-verdict screen. opts: minutes (until next review), idk,
// scheduleNote (string appended after the interval, or null),
// feedback (the grader's markdown note, when a grader wrote one), and
// extras (host-built nodes placed after the answer compare, e.g. the
// chat handoff, whose provider URLs only a server can compose).
export function verdictView(card, verdict, userAnswer, opts, {onNext, onPause}) {
  const frag = document.createDocumentFragment();
  frag.appendChild(studyNav(card, onPause));

  const right = verdict === "right";
  const block = el("section", "verdict-block");
  const mark = el("span", "verdict-mark " + (right ? "verdict-mark-right" : "verdict-mark-wrong"));
  mark.appendChild(icon(right ? "check" : "x"));
  block.appendChild(mark);
  block.appendChild(el("h1", "verdict-headline", right ? "Right." : "Not yet."));
  const sub = el("p", "verdict-sub", "Next review in ");
  sub.appendChild(el("strong", null, humanMinutes(opts.minutes)));
  if (opts.scheduleNote) sub.appendChild(document.createTextNode(opts.scheduleNote));
  block.appendChild(sub);
  frag.appendChild(block);

  const question = el("section", "result-section");
  question.appendChild(sectionEyebrow("The question"));
  question.appendChild(promptNode(card));
  frag.appendChild(question);

  const sections = compareSections(card, userAnswer, {
    idk: opts.idk,
    userLabel: "What you wrote",
    modelLabel: "Model answer",
  });
  for (const section of sections) frag.appendChild(section);

  // A grader that wrote prose gets its own section, markdown-rendered
  // like the prompt. Deterministic grading writes none.
  if (opts.feedback) {
    const fb = el("section", "result-section");
    fb.appendChild(sectionEyebrow("Feedback"));
    const body = el("div", "feedback-body prose");
    body.innerHTML = markdownHTML(opts.feedback);
    fb.appendChild(body);
    frag.appendChild(fb);
  }

  // Host-built nodes (the chat handoff, whose provider URLs only a
  // server can compose) sit after the compare, before the actions.
  for (const extra of opts.extras || []) frag.appendChild(extra);

  const actions = el("div", "study-actions next-actions");
  const nextBtn = el("button", "btn btn-primary", "Next card");
  nextBtn.type = "button";
  nextBtn.addEventListener("click", () => onNext());
  const pauseBtn = el("button", "btn btn-quiet", "Pause");
  pauseBtn.type = "button";
  pauseBtn.addEventListener("click", () => onPause());
  actions.appendChild(nextBtn);
  actions.appendChild(pauseBtn);
  frag.appendChild(actions);

  return frag;
}

// The caught-up screen. summary: {nextDueMinutes: number|null}.
// `scope` is the italic beat in the headline and `nothingSchedule` the
// line shown when nothing is queued: only the caller knows whether the
// queue it just emptied was this device's or the account's.
// `backLabel` names wherever the host's Back button goes.
export function caughtUpView(
  summary,
  {onAdd, onBack, scope = "offline", nothingScheduled = null, backLabel = "Back to overview"}
) {
  const section = el("section", "empty-state");
  const h = el("h2", "empty-headline", "All caught up ");
  h.appendChild(el("em", null, scope));
  h.appendChild(document.createTextNode("."));
  section.appendChild(h);
  const minutes = summary.nextDueMinutes;
  section.appendChild(
    el(
      "p",
      "empty-sub",
      minutes === null
        ? nothingScheduled || "Nothing else is scheduled on this device."
        : "The next card comes due in " + humanMinutes(minutes) + "."
    )
  );
  const actions = el("div", "study-actions caughtup-actions");
  const add = el("button", "btn btn-primary", "Add a card");
  add.type = "button";
  add.addEventListener("click", () => onAdd());
  actions.appendChild(add);
  const back = el("button", "btn btn-quiet", backLabel);
  back.type = "button";
  back.addEventListener("click", () => onBack());
  actions.appendChild(back);
  section.appendChild(actions);
  return section;
}

// The add-a-card form: front, back, deck picker (host-supplied decks
// plus the inbox default). Validation lives here; the write is the
// host's onSave({prompt, answer, deck_id}).
const DEFAULT_AUTHOR_BLURB =
  "Saved to this device now, added to your account next time you " +
  "sync. It studies as a reveal-and-self-grade card and is due " +
  "immediately.";

export function authorView({decks}, {onSave, onBack, blurb = null}) {
  const frag = document.createDocumentFragment();

  const nav = el("nav", "study-nav");
  const back = el("button", "offline-linkbtn back");
  back.type = "button";
  back.appendChild(icon("arrow-left", "icon icon-inline"));
  back.appendChild(document.createTextNode(" Back"));
  back.addEventListener("click", () => onBack());
  nav.appendChild(back);
  frag.appendChild(nav);

  const article = el("article", "study-card author-card");
  article.appendChild(sectionEyebrow("Add a card"));
  article.appendChild(
    el("p", "muted offline-author-blurb", blurb || DEFAULT_AUTHOR_BLURB)
  );

  const form = document.createElement("form");
  form.className = "study-form author-form";

  // All three controls sit in .freetext wrappers: the shared forms.css
  // chrome styles them AND keeps the font at 1rem (16px), which is
  // what stops iOS Safari's auto-zoom-on-focus.
  const promptWrap = el("label", "freetext");
  promptWrap.appendChild(el("span", "freetext-label", "Front"));
  const promptTa = document.createElement("textarea");
  promptTa.rows = 4;
  promptTa.placeholder = "The question you want to be asked.";
  promptWrap.appendChild(promptTa);
  form.appendChild(promptWrap);

  const answerWrap = el("label", "freetext");
  answerWrap.appendChild(el("span", "freetext-label", "Back"));
  const answerInput = document.createElement("input");
  answerInput.type = "text";
  answerInput.placeholder = "The canonical answer.";
  answerWrap.appendChild(answerInput);
  form.appendChild(answerWrap);

  const deckWrap = el("label", "freetext");
  deckWrap.appendChild(el("span", "freetext-label", "Deck"));
  const deckSelect = document.createElement("select");
  const inboxOption = document.createElement("option");
  inboxOption.value = "";
  inboxOption.textContent = "inbox (default)";
  deckSelect.appendChild(inboxOption);
  for (const deck of decks) {
    const option = document.createElement("option");
    option.value = String(deck.id);
    option.textContent = deck.display_name || deck.name;
    deckSelect.appendChild(option);
  }
  deckWrap.appendChild(deckSelect);
  form.appendChild(deckWrap);

  const errorLine = el("p", "author-error", "");
  errorLine.setAttribute("role", "alert");
  errorLine.hidden = true;
  form.appendChild(errorLine);

  const actions = el("div", "study-actions");
  const saveBtn = el("button", "btn btn-primary", "Save card");
  saveBtn.type = "submit";
  actions.appendChild(saveBtn);
  form.appendChild(actions);

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    runPending(saveBtn, async () => {
      const prompt = promptTa.value.trim();
      const answer = answerInput.value.trim();
      if (!prompt || !answer) {
        errorLine.textContent = "Both the front and the back are required.";
        errorLine.hidden = false;
        return;
      }
      errorLine.hidden = true;
      await onSave({
        prompt,
        answer,
        deck_id: deckSelect.value ? Number(deckSelect.value) : null,
      });
    });
  });

  article.appendChild(form);
  frag.appendChild(article);
  return frag;
}
