/* PWA install nudge — wires up the banner + modal partial that
 * lives in base.html, surfaces it when appropriate, and disappears
 * after dismiss / install.
 *
 * Browser matrix this handles:
 *   - Chrome/Edge/Brave on Android + desktop: native `beforeinstallprompt`
 *     event → captured + replayed when the user taps Install. Browser
 *     also fires `appinstalled` on success; we auto-hide then.
 *   - Safari on iOS / iPadOS: no programmatic API. We show a modal
 *     with the Share-button → Add-to-Home-Screen instruction.
 *   - Chrome/Edge on iOS (CriOS/EdgiOS in UA): can't install at all
 *     (only the system Safari can). We tell them to open in Safari.
 *   - Anything else / already-installed: hide entirely.
 *
 * Edge cases learned from khmyznikov/pwa-install (MIT, reference only):
 *   - iPadOS 13+ identifies as Macintosh in UA; detect via maxTouchPoints
 *     > 2 + 'serviceWorker' in navigator before treating as desktop.
 *   - Safari sets `navigator.standalone`; everything else uses
 *     `display-mode: standalone` media query. Check both.
 *   - sessionStorage for transient "closed-for-this-tab" + localStorage
 *     for "don't show again" — give the user both granularities. */

const STORAGE_KEY_DISMISSED = "prep:pwa_nudge_dismissed";
const STORAGE_KEY_SESSION_HIDDEN = "prep:pwa_nudge_hidden_session";

function isStandalone() {
  // PWA installed + opened from home screen — `navigator.standalone`
  // is the iOS Safari signal; the media-query is universal.
  try {
    if (window.matchMedia("(display-mode: standalone)").matches) return true;
    if ("standalone" in navigator && navigator.standalone) return true;
  } catch (_) {}
  return false;
}

function isAppleMobile() {
  // iPadOS 13+ ships a desktop UA. The maxTouchPoints > 2 +
  // serviceWorker check is the most reliable signal (khmyznikov uses
  // the same trick).
  const ua = navigator.userAgent;
  if (/iPhone|iPod/.test(ua)) return true;
  if (
    /Mac/.test(ua) &&
    navigator.maxTouchPoints &&
    navigator.maxTouchPoints > 2 &&
    "serviceWorker" in navigator
  ) {
    return true;
  }
  return false;
}

function isAppleMobileNonSafari() {
  // Chrome / Edge on iOS — they CANNOT install a PWA; only the
  // system Safari can. We tell users to open the page in Safari.
  return isAppleMobile() && /CriOS|EdgiOS|FxiOS/.test(navigator.userAgent);
}

function getStorage(key) {
  try {
    return (
      sessionStorage.getItem(key) === "1" || localStorage.getItem(key) === "1"
    );
  } catch (_) {
    return false;
  }
}

function setStoragePersistent(key) {
  try { localStorage.setItem(key, "1"); } catch (_) {}
}

function setStorageSession(key) {
  try { sessionStorage.setItem(key, "1"); } catch (_) {}
}

export function init(opts = {}) {
  // Root partial lives in base.html; bail quietly if it's not on
  // this page (e.g. error pages that strip chrome).
  const root = document.getElementById("pwa-install-root");
  if (!root) return;

  // Hard out: already installed, or already permanently dismissed.
  if (isStandalone()) return;
  if (getStorage(STORAGE_KEY_DISMISSED)) return;
  if (getStorage(STORAGE_KEY_SESSION_HIDDEN)) return;

  // Pieces of the partial.
  const pill = root.querySelector(".pwa-install-pill");
  const dialog = root.querySelector("dialog.pwa-install-dialog");
  if (!pill || !dialog) return;

  const closeBtn = dialog.querySelector(".pwa-install-close");
  const dontShowAgain = dialog.querySelector(".pwa-install-dont-show");
  const installBtn = dialog.querySelector(".pwa-install-action");
  const iosBlock = dialog.querySelector(".pwa-install-ios");
  const androidBlock = dialog.querySelector(".pwa-install-android");
  const iosNonSafariBlock = dialog.querySelector(".pwa-install-ios-non-safari");
  const unsupportedBlock = dialog.querySelector(".pwa-install-unsupported");

  // Detect platform path. Chromium / Android emit beforeinstallprompt
  // — we hold the event until the user taps Install. iOS / iPad-Safari
  // gets the manual Share→Add-to-Home-Screen instructions. iOS non-
  // Safari gets a "use Safari" pointer. Anything else → no nudge.
  let deferredPrompt = null;
  let kind = null; // "android" | "ios" | "ios-non-safari" | "unsupported"

  if (isAppleMobileNonSafari()) {
    kind = "ios-non-safari";
  } else if (isAppleMobile()) {
    kind = "ios";
  }

  // Even if isAppleMobile() returned false, listen for the
  // beforeinstallprompt event — that's Android Chrome's signal AND
  // desktop Chrome's signal. iOS Safari never fires this.
  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    deferredPrompt = event;
    kind = kind || "android"; // covers desktop Chrome too
    showPill();
  });

  // appinstalled fires when the install completes (Chromium browsers).
  // Hide the nudge immediately + remember persistently so a later
  // re-uninstall doesn't bring it back uninvited.
  window.addEventListener("appinstalled", () => {
    setStoragePersistent(STORAGE_KEY_DISMISSED);
    hideAll();
  });

  // iOS Safari has no install event to wait for — show the pill
  // immediately on iOS-eligible pages.
  if (kind === "ios" || kind === "ios-non-safari") {
    showPill();
  }

  // ---- UI handlers --------------------------------------------------

  function showPill() {
    pill.hidden = false;
    pill.classList.add("is-visible");
  }

  function hideAll() {
    pill.hidden = true;
    if (dialog.open) dialog.close();
  }

  pill.addEventListener("click", () => {
    // Render only the platform-relevant block; hide others.
    iosBlock.hidden = kind !== "ios";
    iosNonSafariBlock.hidden = kind !== "ios-non-safari";
    androidBlock.hidden = kind !== "android";
    unsupportedBlock.hidden = !!kind;
    // Show or hide the native-install button depending on whether
    // we have a captured beforeinstallprompt to replay.
    if (installBtn) installBtn.hidden = kind !== "android" || !deferredPrompt;

    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  });

  if (closeBtn) {
    closeBtn.addEventListener("click", () => {
      dialog.close();
      // Hide the pill for the rest of this session, but leave the
      // long-term opt-out for the "Don't show again" link.
      setStorageSession(STORAGE_KEY_SESSION_HIDDEN);
      hideAll();
    });
  }

  if (dontShowAgain) {
    dontShowAgain.addEventListener("click", (e) => {
      e.preventDefault();
      setStoragePersistent(STORAGE_KEY_DISMISSED);
      dialog.close();
      hideAll();
    });
  }

  if (installBtn) {
    installBtn.addEventListener("click", async () => {
      if (!deferredPrompt) return;
      // .prompt() can only be called once per event. After the user
      // picks, we drop the reference; if they refused, beforeinstall-
      // prompt might fire again later (browser-discretion).
      try {
        deferredPrompt.prompt();
        const choice = await deferredPrompt.userChoice;
        if (choice && choice.outcome === "accepted") {
          setStoragePersistent(STORAGE_KEY_DISMISSED);
          dialog.close();
          hideAll();
        }
      } catch (_) {
        // Browser refused to show the prompt (consumed, or stale event
        // after navigation). Fall back to closing the modal — the
        // user can try again from the pill on the next page load.
      } finally {
        deferredPrompt = null;
        installBtn.hidden = true;
      }
    });
  }
}
