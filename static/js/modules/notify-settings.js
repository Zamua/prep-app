// notify-settings — wires the /notify settings page:
//
//   - tz auto-detect (hidden input + visible label)
//   - quiet-hours toggle hides the time pickers when off
//   - SRS mode radio show/hide of digest / when-ready extras
//   - Save prefs (POST /notify/prefs)
//   - Enable on this device (subscribe pushManager + POST /notify/subscribe)
//   - Disable on this device (unsubscribe + POST /notify/unsubscribe)
//   - Send test push with stateful pending/success/error feedback
//
// Element IDs are baked into the template; module reads them at init.
// Config values that vary per request (root_path, vapid_key) are
// passed in via init({rootPath, vapidKey}).

function urlB64ToUint8Array(b64) {
  const padding = "=".repeat((4 - (b64.length % 4)) % 4);
  const base64 = (b64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

function hourFromTime(s, fallback) {
  if (typeof s === "string" && /^\d{2}:\d{2}$/.test(s)) {
    return Number(s.slice(0, 2));
  }
  return fallback;
}

export async function init({rootPath = "", vapidKey = ""} = {}) {
  const form = document.getElementById("notify-form");
  const status = document.getElementById("status");
  const saveBtn = document.getElementById("save-btn");
  const toggleBtn = document.getElementById("toggle-btn");
  const testBtn = document.getElementById("test-btn");
  const tzInput = document.getElementById("tz-input");
  if (!form || !status || !saveBtn || !toggleBtn || !testBtn) return;

  // tz detection — hidden input drives the saved value, visible label
  // shows it to the user so they understand what zone the times use.
  try {
    const detected = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (detected) {
      if (tzInput) tzInput.value = detected;
      const tzDisplay = document.getElementById("tz-display");
      if (tzDisplay) tzDisplay.textContent = detected;
    }
  } catch (e) {
    /* keep server-stored value */
  }

  // Quiet hours opt-in: hide the time pickers when the toggle is off.
  const quietToggle = document.getElementById("quiet-toggle");
  const quietRow = document.getElementById("quiet-row");
  if (quietToggle && quietRow) {
    quietToggle.addEventListener("change", () => {
      quietRow.hidden = !quietToggle.checked;
    });
  }

  function setStatus(msg, kind) {
    status.textContent = msg;
    status.dataset.kind = kind || "";
  }

  // Mode-specific extras hide based on the selected SRS mode. Quiet
  // hours block stays visible — applies to both SRS when-ready AND
  // trivia notifications, so configurable regardless of SRS mode.
  function syncExtras() {
    const mode = (form.querySelector('input[name="mode"]:checked') || {}).value;
    const digestEl = document.getElementById("extras-digest");
    const whenReadyEl = document.getElementById("extras-when-ready");
    if (digestEl) digestEl.hidden = mode !== "digest";
    if (whenReadyEl) whenReadyEl.hidden = mode !== "when-ready";
    form.querySelectorAll(".notify-mode-option").forEach((opt) => {
      opt.classList.toggle("is-selected", opt.querySelector("input").checked);
    });
  }
  form.querySelectorAll('input[name="mode"]').forEach((r) => {
    r.addEventListener("change", syncExtras);
  });

  // ------- Subscription state on THIS device ---------------------
  // Server-rendered `devices` is total across devices; ask the
  // browser directly for local state.
  let reg = null;
  let localSub = null;

  async function loadLocalSub() {
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) return;
    try {
      reg = await navigator.serviceWorker.register(rootPath + "/sw.js", {
        scope: rootPath + "/",
      });
      await navigator.serviceWorker.ready;
      localSub = await reg.pushManager.getSubscription();
    } catch (e) {
      console.warn("SW register failed:", e);
    }
  }

  function renderActions() {
    const subscribed = !!localSub;
    saveBtn.hidden = false;
    toggleBtn.hidden = false;
    testBtn.hidden = !subscribed;
    // Toggle: data-state drives both label visibility (CSS) and the
    // click-handler dispatch (JS). Primary class follows the user's
    // most likely next action — Enable when not subscribed, Save
    // when they already are.
    toggleBtn.dataset.state = subscribed ? "on" : "off";
    toggleBtn.classList.toggle("btn-primary", !subscribed);
    toggleBtn.classList.toggle("btn-quiet", subscribed);
    saveBtn.classList.toggle("btn-primary", subscribed);
    saveBtn.classList.toggle("btn-quiet", !subscribed);
  }

  // ------- Pref save ---------------------------------------------
  function readPrefs() {
    const fd = new FormData(form);
    return {
      mode: fd.get("mode") || "off",
      digest_hour: hourFromTime(fd.get("digest_time"), 9),
      threshold: Number(fd.get("threshold") || 3),
      quiet_hours_enabled: fd.get("quiet_hours_enabled") === "on",
      quiet_start_hour: hourFromTime(fd.get("quiet_start_time"), 22),
      quiet_end_hour: hourFromTime(fd.get("quiet_end_time"), 8),
      tz: fd.get("tz") || "America/New_York",
    };
  }

  saveBtn.addEventListener("click", async () => {
    setStatus("Saving…");
    const r = await fetch(rootPath + "/notify/prefs", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify(readPrefs()),
    });
    if (r.ok) setStatus("Saved.", "ok");
    else setStatus("Save failed (" + r.status + ").", "error");
  });

  // ------- Enable / Disable / Test -------------------------------
  async function doEnable() {
    if (!reg) {
      setStatus("This browser doesn't support web push.", "error");
      return;
    }
    setStatus("Asking for permission…");
    const perm = await Notification.requestPermission();
    if (perm !== "granted") {
      setStatus(
        "Permission denied. You can re-enable in iOS Settings → Notifications → prep.",
        "error"
      );
      return;
    }
    try {
      localSub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlB64ToUint8Array(vapidKey),
      });
    } catch (e) {
      setStatus("Subscribe failed: " + e.message, "error");
      return;
    }
    const r = await fetch(rootPath + "/notify/subscribe", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify(localSub),
    });
    if (r.ok) {
      setStatus("Subscribed on this device.", "ok");
      renderActions();
    } else {
      setStatus("Server rejected the subscription.", "error");
    }
  }

  async function doDisable() {
    if (!localSub) return;
    const endpoint = localSub.endpoint;
    setStatus("Disabling on this device…");
    try {
      await localSub.unsubscribe();
    } catch (e) {
      // Even if pushManager.unsubscribe throws (rare), still tell the
      // server to forget us so the scheduler doesn't keep targeting a
      // dead endpoint.
      console.warn("local unsubscribe failed:", e);
    }
    await fetch(rootPath + "/notify/unsubscribe", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({endpoint}),
    });
    localSub = null;
    setStatus("Disabled on this device.", "ok");
    renderActions();
  }

  toggleBtn.addEventListener("click", async () => {
    if (toggleBtn.dataset.state === "on") await doDisable();
    else await doEnable();
  });

  testBtn.addEventListener("click", async () => {
    // The button owns its own feedback — pending / success / error
    // states swap inline via the `is-*` class on the button. CSS
    // makes the pending state visually inert (pointer-events:none)
    // so we don't need the HTML disabled attribute (which conflicts
    // with the colored success/error backgrounds via .btn:disabled).
    testBtn.classList.remove("is-success", "is-error");
    testBtn.classList.add("is-pending");
    let result = null;
    try {
      const r = await fetch(rootPath + "/notify/test", {method: "POST"});
      result = await r.json();
    } catch (e) {
      result = {sent: 0};
    }
    testBtn.classList.remove("is-pending");
    testBtn.classList.add(result && result.sent > 0 ? "is-success" : "is-error");
    setTimeout(() => {
      testBtn.classList.remove("is-success", "is-error");
    }, 2400);
  });

  // ------- Boot --------------------------------------------------
  await loadLocalSub();
  renderActions();
}
