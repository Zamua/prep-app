// Progressive enhancement for forms that should NOT cause a navigation
// when submitted. Without this, a form POST→303 round-trip puts the
// page mid-reload; if the user taps another link before the reload
// completes, iOS PWA queues or drops the second tap. The pattern
// shows up as "tap a preset, then tap the back link, nothing happens
// — but a slow second tap works." (2026-05-07 19:51 UTC)
//
// Declarative usage:
//   <form data-submit-ajax method="post" action="/foo">
//     ...
//     <button type="submit" name="x" value="42">42</button>
//   </form>
//
// Optional: `data-ajax-update` on the form points at a CSS selector
// whose textContent should be updated after success. The JS uses the
// first matching `data-ajax-text-from` attribute on the SUBMITTED
// button (or the form, fallback) to decide what to render — see
// renderIntervalText / renderSessionSizeText below.
//
// On failure (non-2xx, network error), falls through to a normal
// form.submit() so the no-JS path still works.

// Each renderer takes the submitted FormData and returns the new
// display text for `data-ajax-update` (or null to skip the update).
// Centralized here so the template only carries the renderer name.
function renderIntervalText(data) {
  const m = parseInt(data.get("minutes"), 10);
  if (Number.isNaN(m) || m <= 0) return null;
  return m < 60 ? `every ${m}m` : `every ${m / 60}h`;
}

function renderSessionSizeText(data) {
  const v = parseInt(data.get("size"), 10);
  if (Number.isNaN(v) || v <= 0) return null;
  return `${v} card${v === 1 ? "" : "s"}`;
}

const RENDERERS = {
  interval: renderIntervalText,
  "session-size": renderSessionSizeText,
};

export function attachDeclarative(root = document) {
  root.querySelectorAll("form[data-submit-ajax]").forEach((form) => {
    if (form.dataset.boundAjax) return;
    form.dataset.boundAjax = "1";

    form.addEventListener("submit", async (e) => {
      // Identify which submit button was clicked. Browsers expose this
      // via `event.submitter` for form-submit events.
      const submitter = e.submitter;
      if (!submitter) return; // shouldn't happen, but fall through to default
      e.preventDefault();

      const data = new FormData(form);
      // FormData includes the submitter's name/value automatically when
      // the submit fires, but only if we let the browser handle it. We
      // intercepted, so add it manually.
      if (submitter.name) {
        data.set(submitter.name, submitter.value);
      }

      try {
        const r = await fetch(form.action, {
          method: form.method || "POST",
          body: data,
          credentials: "same-origin",
        });
        if (!r.ok && r.status !== 303) {
          throw new Error(`http ${r.status}`);
        }
      } catch (_err) {
        // Network / server error → fall back to the regular submit
        // path so the user sees the normal error page rather than a
        // silent failure.
        form.removeAttribute("data-submit-ajax");
        form.submit();
        return;
      }

      // Update the in-place text indicator if the form opted in.
      const updateSelector = form.dataset.ajaxUpdate;
      const renderer = RENDERERS[form.dataset.ajaxRenderer];
      if (updateSelector && renderer) {
        const target = document.querySelector(updateSelector);
        const text = renderer(data);
        if (target && text) target.textContent = text;
      }

      // Mark the chosen preset visually (matches the server-side
      // `notif-preset-active` toggle without a page reload). Only
      // applies when the form lives inside a row of presets.
      const presetClass = form.dataset.ajaxPresetActiveClass;
      if (presetClass && submitter) {
        form.querySelectorAll(`.${presetClass}`).forEach((el) => el.classList.remove(presetClass));
        submitter.classList.add(presetClass);
      }

      // Binary-toggle support (pin/unpin, pause/resume, suspend/unsuspend):
      //   data-ajax-toggle-target=<selector>     element whose class flips
      //   data-ajax-toggle-class=<class>         the class flipped
      //   data-ajax-toggle-label-target=<selector> optional element whose
      //                                          textContent flips
      //   data-ajax-toggle-label-on=<text>       label when class is ON
      //   data-ajax-toggle-label-off=<text>      label when class is OFF
      //   data-ajax-flip-hidden=<input-name>     a hidden input whose
      //                                          value should flip on/off
      //                                          so the next submit
      //                                          toggles back.
      const toggleClass = form.dataset.ajaxToggleClass;
      const toggleTargetSel = form.dataset.ajaxToggleTarget;
      if (toggleClass && toggleTargetSel) {
        // `closest:.foo` walks UP from the form (handy when each row
        // has its own form, like per-card suspend/unsuspend); plain
        // selectors do form-then-document scan.
        let target;
        if (toggleTargetSel.startsWith("closest:")) {
          target = form.closest(toggleTargetSel.slice("closest:".length));
        } else {
          target =
            form.querySelector(toggleTargetSel) || document.querySelector(toggleTargetSel);
        }
        if (target) {
          target.classList.toggle(toggleClass);
          const labelSel = form.dataset.ajaxToggleLabelTarget;
          const labelOn = form.dataset.ajaxToggleLabelOn;
          const labelOff = form.dataset.ajaxToggleLabelOff;
          if (labelSel && labelOn !== undefined && labelOff !== undefined) {
            const labelEl =
              form.querySelector(labelSel) || document.querySelector(labelSel);
            if (labelEl) {
              labelEl.textContent = target.classList.contains(toggleClass) ? labelOn : labelOff;
            }
          }
        }
      }
      const flipName = form.dataset.ajaxFlipHidden;
      if (flipName) {
        const input = form.querySelector(`input[type="hidden"][name="${flipName}"]`);
        if (input) input.value = input.value === "on" ? "off" : "on";
      }

      // Close the parent <details> popover so the user gets a "yep,
      // saved" visual confirmation. Without this they'd see the
      // popover linger after the action.
      const details = form.closest("details");
      if (details) details.removeAttribute("open");
    });
  });
}
