// Service worker for the prep-app PWA. Minimal: just receive `push`
// events and surface them via `registration.showNotification`. No fetch
// caching — the app is small and on-tailnet, no offline use case.
//
// Scope is set at register time to ROOT_PATH (e.g. /prep-staging/) so
// the SW controls every page under the app.

self.addEventListener("install", (event) => {
  // Activate immediately on first install — no skipWaiting tricks
  // needed for a single-controller app.
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

// SCOPE is the SW's mount path — e.g. "/prep/" on prod, "/prep-staging/"
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
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      // Reuse an existing tab if it's already on our origin.
      for (const c of wins) {
        if (c.url.includes(target) && "focus" in c) return c.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow(target);
    })
  );
});
