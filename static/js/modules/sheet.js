// Bottom-sheet drawer for short focused inputs (snooze duration,
// mute duration). The sheet is a single per-page <dialog data-sheet>
// element rendered alongside the session list; trigger buttons carry
// data-sheet-open="<sheet-id>" + data-sheet-action="<POST url>" +
// data-sheet-title="…" + data-sheet-deck="…" to populate the sheet
// for the specific row being acted on.
//
// Why <dialog> rather than a free <div>:
//   - Native ::backdrop + showModal() handles input trapping +
//     ESC-to-close out of the box.
//   - Plays nicely with the existing data-dialog pattern; we extend
//     rather than parallel it.
//
// The sheet's <form> is reused across triggers — we just rewrite its
// `action` attribute on open. The form posts a `preset` field (chip
// row) OR a `custom` + `unit` pair (free-form), matching
// prep.web.durations.parse_until.

const OPEN_ATTR = "data-sheet-open";
const ACTION_ATTR = "data-sheet-action";
const TITLE_ATTR = "data-sheet-title";
const DECK_ATTR = "data-sheet-deck";
const SUBTITLE_ATTR = "data-sheet-subtitle";
// Triggers that want the "Wake now" chip in the sheet (used by the
// Snoozed list to let the user clear an active snooze) set this to
// "1". The chip itself lives in the sheet template tagged with
// .sheet-wake-only; CSS hides it unless the dialog carries .has-wake.
const SHOW_WAKE_ATTR = "data-sheet-show-wake";

function setupTriggers(root) {
  root.addEventListener("click", (ev) => {
    const trigger = ev.target.closest(`[${OPEN_ATTR}]`);
    if (!trigger) return;
    ev.preventDefault();
    const sheetId = trigger.getAttribute(OPEN_ATTR);
    const sheet = document.getElementById(sheetId);
    if (!sheet) return;
    const action = trigger.getAttribute(ACTION_ATTR);
    const title = trigger.getAttribute(TITLE_ATTR) || "";
    const deck = trigger.getAttribute(DECK_ATTR) || "";
    const subtitle = trigger.getAttribute(SUBTITLE_ATTR) || "";

    // Close any sibling overflow popover the trigger lives inside so
    // the sheet is the only thing visible on screen.
    const owningDetails = trigger.closest("details[open]");
    if (owningDetails) owningDetails.removeAttribute("open");

    const form = sheet.querySelector("form[data-sheet-form]");
    if (form && action) form.setAttribute("action", action);
    const titleEl = sheet.querySelector("[data-sheet-title-out]");
    if (titleEl) {
      titleEl.textContent = deck ? `${title} ${deck}` : title;
    }
    const subtitleEl = sheet.querySelector("[data-sheet-subtitle-out]");
    if (subtitleEl) subtitleEl.textContent = subtitle;

    // Reset custom input + active chip state on every open.
    if (form) {
      const customInput = form.querySelector("input[name=custom]");
      if (customInput) customInput.value = "";
    }
    sheet.querySelectorAll(".sheet-chip[aria-pressed=true]").forEach((c) => {
      c.setAttribute("aria-pressed", "false");
    });

    // Toggle the Wake-now chip per the trigger. .has-wake on the
    // dialog reveals .sheet-wake-only descendants via the CSS.
    sheet.classList.toggle("has-wake", trigger.getAttribute(SHOW_WAKE_ATTR) === "1");

    sheet.showModal();
  });
}

function setupSheets(root) {
  root.querySelectorAll("dialog[data-sheet]").forEach((sheet) => {
    // Backdrop click → close. Native <dialog>'s ::backdrop click goes
    // to the dialog itself, so we treat a click on the dialog
    // (i.e. NOT one of its children) as a close.
    sheet.addEventListener("click", (ev) => {
      if (ev.target === sheet) sheet.close();
    });

    // Cancel button (data-sheet-cancel) closes without submitting.
    sheet.addEventListener("click", (ev) => {
      if (ev.target.closest("[data-sheet-cancel]")) {
        ev.preventDefault();
        sheet.close();
      }
    });

    // Chip-press toggles aria-pressed on the form's chip group AND,
    // if it's a preset chip with a hidden submit, submits the form
    // (1-tap behavior). Custom chips with type=submit + name=preset
    // submit immediately when clicked. The custom-duration row uses
    // a separate submit button.
    sheet.addEventListener("click", (ev) => {
      const chip = ev.target.closest(".sheet-chip");
      if (!chip) return;
      sheet
        .querySelectorAll(".sheet-chip")
        .forEach((c) => c.setAttribute("aria-pressed", "false"));
      chip.setAttribute("aria-pressed", "true");
    });
  });
}

export function attachDeclarative(root = document) {
  setupTriggers(root);
  setupSheets(root);
}
