// Service worker: cache the app shell (API always goes to network) + Web Push.
// Caches are shared across the whole origin, so name them per scope: a /beta/
// preview must never delete production's cache (or vice versa).
const SCOPE = self.registration.scope;
const SHELL = "mesh-v53@" + SCOPE;
const isRoot = new URL(SCOPE).pathname === "/";
// Relative to this worker, so the same build works at / and in a /beta/ preview.
const FILES = ["./", "index.html", "app.js", "style.css", "manifest.webmanifest",
  "vendor/uPlot.iife.min.js", "vendor/uPlot.min.css", "vendor/leaflet.js", "vendor/leaflet.css", "vendor/qrcode.js", "icons/icon-192.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys
      .filter((k) => k !== SHELL && (k.endsWith("@" + SCOPE) || (isRoot && !k.includes("@"))))
      .map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/api/") || e.request.method !== "GET") return;
  // Network first so deploys show up straight away; cache when offline.
  e.respondWith(fetch(e.request)
    .then((r) => { const copy = r.clone(); caches.open(SHELL).then((c) => c.put(e.request, copy)); return r; })
    .catch(() => caches.match(e.request).then((r) => r || caches.match("index.html"))));
});

self.addEventListener("push", (e) => {
  const d = e.data ? e.data.json() : { title: "MeshCore", body: "New message" };
  if (d.badge != null) { try { self.navigator.setAppBadge?.(d.badge); } catch { /* unsupported */ } }
  e.waitUntil(self.registration.showNotification(d.title, {
    body: d.body, tag: d.tag, renotify: true, icon: "/icons/icon-192.png",
    badge: "/icons/icon-192.png", data: { url: d.url || "/" },
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const target = e.notification.data.url;
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
    for (const w of wins) { w.navigate(target); return w.focus(); }
    return self.clients.openWindow(target);
  }));
});
