// Service worker: cache the app shell (API always goes to network) + Web Push.
// Caches are shared across the whole origin, so name them per scope: a /beta/
// preview must never delete production's cache (or vice versa).
const SCOPE = self.registration.scope;
const SHELL = "mesh-v66@" + SCOPE;
const isRoot = new URL(SCOPE).pathname === "/";
// Relative to this worker, so the same build works at / and in a /beta/ preview.
const FILES = ["./", "index.html", "app.js", "style.css", "manifest.webmanifest",
  "vendor/uPlot.iife.min.js", "vendor/uPlot.min.css", "vendor/leaflet.js", "vendor/leaflet.css", "vendor/qrcode.js", "vendor/chess.min.js", "icons/icon-192.png",
  "fonts/CommitMono-400-Regular.woff2", "fonts/CommitMono-700-Regular.woff2"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys
      .filter((k) => k !== SHELL && k !== TILES && (k.endsWith("@" + SCOPE) || (isRoot && !k.includes("@"))))
      .map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

// Map tiles and fonts (OpenFreeMap, OSM, or a self-hosted .pmtiles) get their own
// cache, capped, so the places you've looked at still show offline without the
// shell cache growing forever.
const TILES = "mesh-tiles@" + SCOPE, TILE_CAP = 2000;
const isTile = (url) => /(^|\.)(openfreemap\.org|openstreetmap\.org)$/.test(url.hostname) || url.pathname.endsWith(".pmtiles");
async function trimTiles() {
  const c = await caches.open(TILES), keys = await c.keys();
  for (const k of keys.slice(0, Math.max(0, keys.length - TILE_CAP))) await c.delete(k);   // oldest first
}
let trimming = null;

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  if (isTile(url)) {
    if (e.request.headers.has("range")) return;   // pmtiles byte ranges: straight to network
    // Tiles and glyphs never change at a given URL (OpenFreeMap dates its tile
    // paths): cache first. The TileJSON index does change: network first.
    const immutable = /\.(pbf|png|mvt)$/.test(url.pathname);
    const save = (c, r) => {
      if (r.ok || r.type === "opaque") { c.put(e.request, r.clone()); trimming ||= trimTiles().finally(() => { trimming = null; }); }
      return r;
    };
    e.respondWith(caches.open(TILES).then((c) => immutable
      ? c.match(e.request).then((hit) => hit || fetch(e.request).then((r) => save(c, r)))
      : fetch(e.request).then((r) => save(c, r)).catch(() => c.match(e.request).then((hit) => hit || Response.error()))));
    return;
  }
  if (url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;
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
