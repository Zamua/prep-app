// components.js: the dashboard views as standalone functions. Each
// takes an overview (the DeckSource payload) plus callbacks and
// returns detached DOM; no storage access, no navigation, no fetch.
// The hosts own state, mount the returned nodes, and supply the URLs
// and menus only a server-backed surface can compose.
//
// Every class here is styled by the existing stylesheet
// (static/css/components/deck-list.css, mastery-bar.css,
// empty-state.css), so both surfaces are the same screen. That the
// classes and the rules stay in step is pinned by
// tests/e2e/test_dashboard_components_e2e.py, which reads the CSS
// files.
//
// Copy that only one surface can truthfully say ("or generate a fresh
// batch", the offline status line) is a per-call option with a
// default, never a branch on which host is calling. What a surface
// cannot offer is omitted by DATA: a deck with no href renders no
// href, a deck with no trivia_stats renders the due/total row.

import {el, icon, prelude, sectionEyebrow, humanMinutes} from "../study/dom.js";

// Deck-row numerals. Past the twelfth the row renders a blank
// numeral rather than a fallback glyph: the numeral is decoration
// (aria-hidden), and the name is what identifies the row.
const ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII"];

// ---- prelude -----------------------------------------------------------

const NEW_USER_LEDE =
  "Type a topic and prep turns it into a deck of flashcards. Each card " +
  "re-surfaces on a forgetting curve: the ones you remember fade, the " +
  "ones you don't quietly come back around.";

const NEW_USER_LEDE_NO_AGENT =
  "Type a topic and prep turns it into a deck of flashcards, or write " +
  "the cards yourself. Each card re-surfaces on a forgetting curve: the " +
  "ones you remember fade, the ones you don't quietly come back around.";

const RETURNING_LEDE =
  "Pick a deck to study what's due. Cards re-surface on a forgetting " +
  "curve: the more you remember, the longer they sleep.";

const RETURNING_LEDE_AGENT =
  "Pick a deck to study what's due, or generate a fresh batch. Cards " +
  "re-surface on a forgetting curve: the more you remember, the longer " +
  "they sleep.";

// The dashboard prelude. `isNewUser` defaults to "no decks", which is
// all an offline device can know; a host that also counts sessions
// passes its own answer. `status` is the one line a surface adds
// about itself (the offline shell says it is reading a snapshot):
// muted, under the lede, in place of separate branding.
export function preludeView(
  overview,
  {
    isNewUser = null,
    agentAvailable = false,
    eyebrow = null,
    headStart = "A standing library",
    headEm = "of questions",
    lede = null,
    status = null,
  } = {}
) {
  const isNew = isNewUser === null ? overview.decks.length === 0 : Boolean(isNewUser);
  const defaultLede = isNew
    ? agentAvailable
      ? NEW_USER_LEDE
      : NEW_USER_LEDE_NO_AGENT
    : agentAvailable
      ? RETURNING_LEDE_AGENT
      : RETURNING_LEDE;
  const section = prelude(
    eyebrow || (isNew ? "Welcome" : "The decks"),
    headStart,
    headEm,
    lede || defaultLede,
    {lineBreak: true}
  );
  if (status) section.appendChild(el("p", "muted dashboard-status", status));
  return section;
}

// ---- deck list ---------------------------------------------------------

function masteryBar(stats) {
  const total = stats.total || 1;
  const mastered = (100 * stats.mastered) / total;
  const wrong = (100 * stats.wrong) / total;
  const bar = el("span", "mastery-bar mastery-bar--mini");
  bar.setAttribute("role", "progressbar");
  bar.setAttribute("aria-valuenow", String(Math.round(mastered)));
  bar.setAttribute("aria-valuemin", "0");
  bar.setAttribute("aria-valuemax", "100");
  const right = el("span", "mastery-bar__fill mastery-bar__fill--right");
  right.style.width = mastered.toFixed(2) + "%";
  const missed = el("span", "mastery-bar__fill mastery-bar__fill--wrong");
  missed.style.left = mastered.toFixed(2) + "%";
  missed.style.width = wrong.toFixed(2) + "%";
  bar.appendChild(right);
  bar.appendChild(missed);
  return bar;
}

function masteryMini(stats) {
  const wrap = el("span", "deck-mastery-mini");
  wrap.appendChild(masteryBar(stats));
  const label = el("span", "deck-mastery-mini-label");
  label.appendChild(el("span", "stat-value", String(stats.mastered)));
  label.appendChild(el("span", "stat-label", "/" + stats.total + " mastered"));
  wrap.appendChild(label);
  return wrap;
}

function deckStats(deck) {
  const stats = el("span", "deck-stats");
  const due = el("span", "stat-due" + (deck.due > 0 ? " is-active" : ""));
  due.appendChild(el("span", "stat-value", String(deck.due)));
  due.appendChild(el("span", "stat-label", "due now"));
  stats.appendChild(due);
  const divider = el("span", "stat-divider", "·");
  divider.setAttribute("aria-hidden", "true");
  stats.appendChild(divider);
  const total = el("span", "stat-total");
  total.appendChild(el("span", "stat-value", String(deck.total)));
  total.appendChild(el("span", "stat-label", "in deck"));
  stats.appendChild(total);
  return stats;
}

// One deck row. `index` is 1-based per section, so each section's
// numerals start at I. The inline custom property drives the
// entry-animation stagger and the view-transition name pairs a card's
// old and new position when it moves between sections.
function deckCard(deck, index, {deckHref, deckMenu}) {
  const item = el("li", "deck-card" + (deck.pinned ? " deck-card-pinned" : ""));
  item.style.setProperty("--i", String(index));
  item.style.viewTransitionName = "deck-" + deck.id;

  const link = el("a", "deck-link");
  const href = deckHref(deck);
  if (href) link.href = href;

  const numeral = el("span", "deck-numeral", ROMAN[index - 1] || "");
  numeral.setAttribute("aria-hidden", "true");
  link.appendChild(numeral);

  const body = el("span", "deck-body");
  body.appendChild(
    el("span", "deck-type-eyebrow deck-type-eyebrow-" + deck.deck_type, deck.deck_type)
  );
  const name = el("span", "deck-name", deck.display_name);
  if (deck.pinned) {
    // The mark is a separate word, not a suffix: without the space it
    // sits against the last glyph of the name.
    name.appendChild(document.createTextNode(" "));
    const mark = el("span", "deck-pin-mark");
    mark.setAttribute("aria-label", "pinned");
    mark.setAttribute("title", "Pinned to top");
    mark.appendChild(icon("push-pin", "icon icon-inline"));
    name.appendChild(mark);
  }
  body.appendChild(name);
  body.appendChild(
    deck.deck_type === "trivia" && deck.trivia_stats
      ? masteryMini(deck.trivia_stats)
      : deckStats(deck)
  );
  link.appendChild(body);
  item.appendChild(link);

  // The row's overflow menu (pin, delete, export) is host-built: every
  // row of it is a server route, so only a server-backed host can
  // compose one. Same seam as the study verdict's `extras`.
  const menu = deckMenu(deck);
  if (menu) item.appendChild(menu);
  return item;
}

function deckOl(decks, opts) {
  const list = el("ol", "deck-list");
  list.setAttribute("role", "list");
  decks.forEach((deck, i) => list.appendChild(deckCard(deck, i + 1, opts)));
  return list;
}

// An action is a link when it goes somewhere and a button when it
// does something here. A surface with no page to navigate to (the
// offline shell's authoring entry) keeps the same pill either way.
function actionPill(action) {
  const pill = el(action.onClick ? "button" : "a", "deck-action-pill");
  if (action.onClick) {
    pill.type = "button";
    pill.addEventListener("click", () => action.onClick());
  } else if (action.href) {
    pill.href = action.href;
  }
  if (action.glyph) {
    const glyph = el("span", "deck-action-icon", action.glyph);
    glyph.setAttribute("aria-hidden", "true");
    pill.appendChild(glyph);
  } else if (action.icon) {
    pill.appendChild(icon(action.icon, "icon icon-inline"));
  }
  pill.appendChild(el("span", null, action.label));
  return pill;
}

const DEFAULT_EMPTY = {
  headStart: "Start with ",
  headEm: "one deck",
  sub:
    "Pick a topic you want to remember: anything from world capitals to " +
    "a paper you just read. Prep does the rest.",
  ctaLabel: "Create your first deck",
  ctaHref: null,
  ctaOnClick: null,
};

function emptyState(copy) {
  const section = el("section", "empty-state index-empty-state");
  const headline = el("p", "empty-headline", copy.headStart);
  headline.appendChild(el("em", null, copy.headEm));
  headline.appendChild(document.createTextNode("."));
  section.appendChild(headline);
  section.appendChild(el("p", "empty-sub", copy.sub));
  // Same rule as the action pills: the call decides whether the one
  // action is somewhere to go or something to do here.
  const cta = el(
    copy.ctaOnClick ? "button" : "a",
    "btn btn-primary index-empty-cta",
    copy.ctaLabel
  );
  if (copy.ctaOnClick) {
    cta.type = "button";
    cta.addEventListener("click", () => copy.ctaOnClick());
  } else if (copy.ctaHref) {
    cta.href = copy.ctaHref;
  }
  section.appendChild(cta);
  return section;
}

// The deck lists: the pinned group, then the rest (or the first-deck
// empty state). `deckHref` and `deckMenu` are how a host attaches the
// routes it owns; both default to nothing, which is the honest render
// on a surface with no deck pages. `actions` is the pill row under
// the eyebrow, `hasSessions` only changes that eyebrow's wording.
export function deckListView(
  overview,
  {
    deckHref = () => null,
    deckMenu = () => null,
    actions = [],
    hasSessions = false,
    isNewUser = null,
    empty = null,
  } = {}
) {
  const root = el("div");
  root.id = "deck-lists";
  const pinned = overview.decks.filter((d) => d.pinned);
  const rest = overview.decks.filter((d) => !d.pinned);
  const opts = {deckHref, deckMenu};

  if (pinned.length) {
    const section = el("section", "decks-section decks-section-pinned");
    section.appendChild(sectionEyebrow("Pinned"));
    section.appendChild(deckOl(pinned, opts));
    root.appendChild(section);
  }

  const isNew =
    isNewUser === null ? overview.decks.length === 0 && !hasSessions : Boolean(isNewUser);
  if (isNew) {
    root.appendChild(emptyState({...DEFAULT_EMPTY, ...(empty || {})}));
    return root;
  }

  const section = el("section", "decks-section");
  section.appendChild(
    sectionEyebrow(pinned.length ? "Other decks" : hasSessions ? "Or pick a deck" : "Your decks")
  );
  if (actions.length) {
    const row = el("div", "deck-actions");
    for (const action of actions) row.appendChild(actionPill(action));
    section.appendChild(row);
  }
  section.appendChild(deckOl(rest, opts));
  root.appendChild(section);
  return root;
}

// ---- due strip ---------------------------------------------------------

// What is due across every deck, with the one action that follows
// from it. Nothing due renders the wait instead of the button, so the
// strip never offers a study session with no cards in it.
export function dueStripView(
  overview,
  {onStudy = null, eyebrow = "Due now", studyLabel = "Study", nothingScheduled = null} = {}
) {
  const section = el("section", "due-strip");
  section.appendChild(sectionEyebrow(eyebrow, overview.due ? overview.due + " due" : null));
  if (overview.due > 0 && onStudy) {
    const actions = el("div", "study-actions due-strip-actions");
    const study = el("button", "btn btn-primary", studyLabel);
    study.type = "button";
    study.addEventListener("click", () => onStudy());
    actions.appendChild(study);
    section.appendChild(actions);
    return section;
  }
  const minutes = overview.nextDueMinutes;
  section.appendChild(
    el(
      "p",
      "muted due-strip-note",
      minutes === null
        ? nothingScheduled || "Nothing is scheduled."
        : "The next card comes due in " + humanMinutes(minutes) + "."
    )
  );
  return section;
}
