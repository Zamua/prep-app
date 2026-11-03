// Schedule for the masthead badge poll. The request is still htmx (the
// badge slot listens for `badge-poll`); this module decides when to send
// that event. A fixed `every 5s` never stops on its own, so a tab whose
// session ended would poll for as long as it stays open.
//
// - 401, 403 and a redirect away from the badge URL: stop. The next
//   thing that polls is the tab becoming visible again (one probe).
// - any other failure: back off exponentially to MAX_MS; the first
//   success resets it.
// - hidden tab: no timer, no request; shown again: one poll at once.

export const BASE_MS = 5000;
export const MAX_MS = 5 * 60 * 1000;

const BADGE_ID = "workflow-badge";
const POLL_EVENT = "badge-poll";

/** The wait before the next poll after `failures` consecutive failures. */
export function backoffMs(failures) {
  return Math.min(BASE_MS * 2 ** Math.min(failures, 30), MAX_MS);
}

/** 'ok' | 'auth' | 'error' for a finished request to `badgePath`. */
export function classify({status, responseURL}, badgePath) {
  if (status === 401 || status === 403) return "auth";
  if (status !== 200) return "error";
  let landed;
  try {
    landed = new URL(responseURL).pathname;
  } catch {
    return "error";
  }
  return landed === badgePath ? "ok" : "auth";
}

/**
 * The poll state machine. `fire` sends one request and the caller reports
 * its outcome; it returns false when there is nothing to poll.
 */
export function createPoller({fire, isHidden, setTimer, clearTimer}) {
  let timer = null;
  let failures = 0;
  let inflight = false;
  let stopped = false;

  function cancel() {
    if (timer !== null) clearTimer(timer);
    timer = null;
  }

  function send() {
    cancel();
    stopped = false;
    inflight = true;
    if (fire() === false) {
      inflight = false;
      stopped = true;
    }
  }

  function schedule(ms) {
    cancel();
    if (stopped || isHidden()) return;
    timer = setTimer(() => {
      timer = null;
      send();
    }, ms);
  }

  return {
    start() {
      if (!isHidden()) send();
    },
    report(outcome) {
      inflight = false;
      if (outcome === "auth") {
        stopped = true;
        cancel();
      } else if (outcome === "ok") {
        failures = 0;
        schedule(BASE_MS);
      } else {
        failures += 1;
        schedule(backoffMs(failures));
      }
    },
    visibility(hidden) {
      if (hidden) {
        cancel();
      } else if (!inflight && timer === null) {
        send();
      }
    },
  };
}

export function init() {
  if (!document.getElementById(BADGE_ID)) return;

  const poller = createPoller({
    fire() {
      const el = document.getElementById(BADGE_ID);
      if (!el) return false;
      el.dispatchEvent(new CustomEvent(POLL_EVENT));
    },
    isHidden: () => document.hidden,
    setTimer: (fn, ms) => setTimeout(fn, ms),
    clearTimer: (id) => clearTimeout(id),
  });

  const finished = (detail) => {
    const requested = detail && detail.pathInfo && detail.pathInfo.requestPath;
    if (!requested || !requested.endsWith("/api/active-workflows-badge")) return null;
    const xhr = detail.xhr || {};
    return classify(
      {status: xhr.status, responseURL: xhr.responseURL || ""},
      new URL(requested, location.href).pathname,
    );
  };

  // A redirect to a sign-in page answers 200 from another URL: keep it
  // out of the badge slot.
  document.addEventListener("htmx:beforeSwap", (e) => {
    const outcome = finished(e.detail);
    if (outcome && outcome !== "ok") e.detail.shouldSwap = false;
  });
  document.addEventListener("htmx:afterRequest", (e) => {
    const outcome = finished(e.detail);
    if (outcome) poller.report(outcome);
  });
  document.addEventListener("visibilitychange", () => poller.visibility(document.hidden));

  // htmx processes the slot on DOMContentLoaded, after module scripts
  // run: an event sent earlier would have no listener.
  if (document.readyState === "complete") poller.start();
  else window.addEventListener("load", () => poller.start(), {once: true});
}
