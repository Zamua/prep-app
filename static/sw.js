// Service worker for the prep-app PWA. Two responsibilities:
//
//   1. Push: receive `push` events and surface them via
//      `registration.showNotification`; focus/navigate on click.
//   2. Offline: precache the /offline shell + its styles/modules/icons
//      at install time, serve the shell as a navigation fallback when
//      the network fails or hangs, and serve precached subresources
//      cache-first. Nothing else is intercepted; the online app's
//      behavior stays byte-identical to a SW-less page.
//
// Scope is set at register time to ROOT_PATH (e.g. /prep-staging/) so
// the SW controls every page under the app.
//
// The BUILD and PRECACHE constants below hold placeholders that the
// /sw.js route (prep/web/pwa.py) substitutes before serving: the
// deterministic build token (lowercase hex) and a JSON array of
// scope-relative URLs to precache. The placeholder spellings must
// appear ONLY at the two definition sites: substitution is a global
// string replace, so writing them out anywhere else (including this
// comment) embeds a second copy of the manifest in the served file.
//
// A new build changes the token, which changes these bytes, which is
// the browser's trigger to install the new SW version.

const BUILD = "__BUILD__";
const PRECACHE = __PRECACHE__;

const CACHE_NAME = "prep-offline-v" + BUILD;
const NAV_TIMEOUT_MS = 4000;

// SCOPE is the SW's mount path: e.g. "/prep/" on prod, "/prep-staging/"
// on staging. registration.scope is an absolute URL like
// "https://host/prep/", so we slice off the host part to get the path
// prefix the rest of the SW uses for icons + fallback URLs.
const SCOPE = (function () {
  try {
    return new URL(self.registration.scope).pathname;
  } catch (e) {
    return "/";
  }
})();

// The shell is fetched and matched at its build-stamped key, never
// bare /offline: the query token pins the shell render to the same
// build whose asset URLs this install stores, so a deploy racing the
// install can never produce a cache whose shell references URLs that
// are not in it (docs/OFFLINE.md, "shell-token construction").
const SHELL_KEY = SCOPE + "offline?build=" + BUILD;

// Version-segment normalization. Asset URLs carry an opaque version
// path segment ("v" + lowercase hex 7-40 chars; legacy pages from
// pre-deploy caches may still hold the old all-digit boot-stamp
// form). Tokens are shaped so they can never collide with a real
// path segment. Normalizing = dropping that segment, so a page
// rendered at one build can fall back to the bytes another build's
// install cached, but only after the network has actually failed.
const VERSION_SEGMENT = /^v([0-9a-f]{7,40}|[0-9]+)$/;

function normalizePath(pathname) {
  return pathname
    .split("/")
    .filter((segment) => !VERSION_SEGMENT.test(segment))
    .join("/");
}

// normalized pathname -> the exact precache URL this install stored.
const NORMALIZED_TO_EXACT = new Map();
for (const url of PRECACHE) {
  try {
    const u = new URL(url, self.location.origin);
    NORMALIZED_TO_EXACT.set(normalizePath(u.pathname), url);
  } catch (e) {
    // A malformed manifest entry must not break SW evaluation.
  }
}

self.addEventListener("install", (event) => {
  // Activate immediately: single-controller app, no waiting phase.
  self.skipWaiting();
  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE_NAME);
      await Promise.all(
        PRECACHE.map(async (url) => {
          // cache: "reload" bypasses the HTTP cache so a deploy can
          // never precache stale bytes.
          const response = await fetch(url, {
            cache: "reload",
            credentials: "same-origin",
          });
          // Load-bearing checks: cache.put happily stores a transient
          // 502 or a redirect, and the SW only reinstalls when /sw.js
          // changes: a poisoned shell would keep serving an error
          // page on every offline cold launch until the next build.
          // Any failing response rejects the whole install; the old
          // SW, with its complete old cache, stays active.
          if (!response.ok || response.redirected) {
            throw new Error(
              "precache failed for " + url + " (status " + response.status +
                (response.redirected ? ", redirected" : "") + ")"
            );
          }
          await cache.put(url, response);
        })
      );
    })()
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      // Drop every prep-offline-* cache from other builds.
      const names = await caches.keys();
      await Promise.all(
        names
          .filter((n) => n.startsWith("prep-offline-") && n !== CACHE_NAME)
          .map((n) => caches.delete(n))
      );
      await self.clients.claim();
    })()
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  let url;
  try {
    url = new URL(request.url);
  } catch (e) {
    return;
  }
  if (url.origin !== self.location.origin) return;
  if (!url.pathname.startsWith(SCOPE)) return;

  if (request.mode === "navigate") {
    // respondWith must be called synchronously, so the shell-presence
    // check happens inside the handler; when the shell is absent the
    // request is forwarded unchanged with no timeout, preserving
    // today's behavior for uncached browsers on slow networks.
    event.respondWith(
      (async () => {
        const cache = await caches.open(CACHE_NAME);
        const shell = await cache.match(SHELL_KEY);
        if (!shell) return fetch(request);
        // Race the network against a timeout. Any response the server
        // produces within the window passes through untouched: 4xx,
        // 5xx, redirects included; an error page from a reachable
        // server is information. Only a rejected fetch (airplane mode)
        // or the timeout (one-bar cellular, DNS blackhole) falls back
        // to the cached shell.
        //
        // The signal is used purely as a timer: navigation requests
        // cannot be reconstructed with a fresh signal (new Request on
        // a mode:"navigate" request throws), so the fetch itself is
        // left un-aborted and simply loses the race.
        const signal = AbortSignal.timeout(NAV_TIMEOUT_MS);
        const timeout = new Promise((_, reject) => {
          signal.addEventListener("abort", () => reject(signal.reason));
        });
        try {
          return await Promise.race([fetch(request), timeout]);
        } catch (e) {
          return shell;
        }
      })()
    );
    return;
  }

  // Non-navigate GETs: only URLs that normalize onto a precached
  // asset are intercepted. Everything else passes through untouched.
  const exactKey = NORMALIZED_TO_EXACT.get(normalizePath(url.pathname));
  if (!exactKey) return;

  event.respondWith(
    (async () => {
      const cache = await caches.open(CACHE_NAME);
      // Exact-URL match first: online, every page stays byte-correct
      // (a new build's URLs are never answered with an older build's
      // bytes while the network can supply the real ones).
      const exact = await cache.match(request);
      if (exact) return exact;
      try {
        return await fetch(request);
      } catch (e) {
        // Network failure only: a page rendered online at another
        // build that loses connectivity mid-session still resolves
        // its subresources from the cache this device has.
        const fallback = await cache.match(exactKey);
        if (fallback) return fallback;
        throw e;
      }
    })()
  );
});

self.addEventListener("push", (event) => {
  let data = { title: "prep", body: "" };
  if (event.data) {
    try {
      data = event.data.json();
    } catch (e) {
      data = { title: "prep", body: event.data.text() };
    }
  }
  const title = data.title || "prep";
  const options = {
    body: data.body || "",
    icon: data.icon || SCOPE + "static/pwa/icon-192.png",
    badge: data.badge || SCOPE + "static/pwa/icon-192.png",
    // Server now sends URLs already prefixed with ROOT_PATH; if a
    // legacy push arrives without the prefix, fall back to scope.
    data: { url: data.url || SCOPE },
    tag: data.tag || "prep-default",
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || SCOPE;
  event.waitUntil(
    (async () => {
      const wins = await self.clients.matchAll({
        type: "window",
        includeUncontrolled: true,
      });
      // Best case: there's already a tab on the target URL: focus it.
      for (const c of wins) {
        if (c.url.includes(target) && "focus" in c) return c.focus();
      }
      // Otherwise: focus the most recent PWA tab and navigate it to
      // the target. iOS standalone PWAs treat clients.openWindow() as
      // "focus start_url" (not "open this URL"), so without this step
      // tapping a notification dropped the user on whatever tab was
      // open: typically the notification log: instead of the card
      // the push pointed at. Navigating an already-focused client is
      // the workaround that lands on the right URL.
      for (const c of wins) {
        if ("navigate" in c && "focus" in c) {
          await c.focus();
          return c.navigate(target);
        }
      }
      // Last resort: no PWA tab is open at all: open a fresh window.
      if (self.clients.openWindow) return self.clients.openWindow(target);
    })()
  );
});
