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
    icon: data.icon || "/prep-staging/static/pwa/icon-192.png",
    badge: data.badge || "/prep-staging/static/pwa/icon-192.png",
    data: { url: data.url || "/prep-staging/" },
    tag: data.tag || "prep-default",
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/prep-staging/";
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
