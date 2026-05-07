// Polling helper for workflow-tracking pages (transform, plan, trivia
// generation). Wraps the setInterval + visibilitychange + cache-bust
// pattern that these pages all need. Pauses on tab-hidden (iOS PWA
// suspends timers anyway), fires an immediate tick on tab-visible to
// catch state changes that happened while suspended.
//
// Usage from JS:
//   import {startPoller} from "@/modules/poller.js";
//   const p = startPoller({
//     url: "/transform/abc/status",
//     intervalMs: 2000,
//     onTick: (data) => { ... },     // called with parsed JSON each poll
//     stopWhen: (data) => done,      // truthy → stop polling
//   });
//
// startPoller returns a controller {stop()} so callers can tear it
// down on navigation. By default the helper handles its own lifecycle:
// stops on stopWhen, swallows fetch errors with backoff, re-arms on
// visibility change.

const DEFAULT_INTERVAL = 2000;
const ERROR_BACKOFF_MS = 3000;

export function startPoller({url, intervalMs = DEFAULT_INTERVAL, onTick, stopWhen}) {
  if (!url || typeof onTick !== "function") {
    throw new Error("poller: url + onTick are required");
  }
  let handle = null;
  let stopped = false;

  async function tick() {
    if (stopped) return;
    try {
      // Cache-buster — defensive against any HTTP cache between us and
      // the origin returning stale responses (seen on iOS Safari).
      const r = await fetch(url + (url.includes("?") ? "&" : "?") + "_=" + Date.now(), {
        cache: "no-store",
        credentials: "same-origin",
      });
      if (!r.ok) return;
      const data = await r.json();
      onTick(data);
      if (stopWhen && stopWhen(data)) {
        stop();
        return;
      }
    } catch (_e) {
      /* swallow; we'll try next tick or visibility-resume */
    }
  }

  function start() {
    if (handle || stopped) return;
    handle = setInterval(tick, intervalMs);
  }
  function pause() {
    if (handle) {
      clearInterval(handle);
      handle = null;
    }
  }
  function stop() {
    stopped = true;
    pause();
    document.removeEventListener("visibilitychange", onVisibility);
  }
  function onVisibility() {
    if (document.hidden) {
      pause();
    } else {
      // Page just became visible — fire an immediate poll to catch
      // any state change that happened while the tab was suspended,
      // then re-arm the interval.
      tick();
      start();
    }
  }

  document.addEventListener("visibilitychange", onVisibility);
  start();
  // Fire an immediate poll so the UI doesn't sit on its initial
  // empty state for one full interval.
  tick();

  return {stop, tick};
}

// Declarative wiring: any element with data-poll-url and an inline
// data-poll-handler="<window-fn-name>" gets a poller automatically.
// The inline handler is the page's existing tick callback (kept
// per-page so each surface owns its own DOM updates).
export function attachDeclarative(root = document) {
  root.querySelectorAll("[data-poll-url]").forEach((el) => {
    const url = el.dataset.pollUrl;
    const interval = parseInt(el.dataset.pollInterval || DEFAULT_INTERVAL, 10);
    const handlerName = el.dataset.pollHandler;
    const stopName = el.dataset.pollStopWhen;
    const onTick = handlerName && window[handlerName];
    if (typeof onTick !== "function") return;
    const stopWhen = stopName && typeof window[stopName] === "function" ? window[stopName] : undefined;
    const controller = startPoller({url, intervalMs: interval, onTick, stopWhen});
    el._pollerController = controller;
  });
}
