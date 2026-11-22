// NoSePol service worker: makes the page installable as an app, and opens it offline.
// Network first, always: the page comes fresh from the server whenever it can, so an
// update shows on the next load. The cache only answers when the network fails.
// /api (live buses, the stream) and other sites (map tiles, Leaflet, fonts) are never
// touched: they go straight to the network.
const SHELL = "nosepol-shell-v1";
const FILES = ["/", "/manifest.webmanifest", "/favicon.svg", "/icons/app-default-192.png", "/icons/app-default-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== SHELL).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  const page = req.mode === "navigate";
  e.respondWith(fetch(req).then(res => {
    // keep the copy of the page (and the shell files) current for when we're offline
    if (res.ok && (page || FILES.includes(url.pathname))) {
      const copy = res.clone();
      caches.open(SHELL).then(c => c.put(page ? "/" : req, copy));
    }
    return res;
  }).catch(() => caches.match(page ? "/" : req, { ignoreSearch: true })
    .then(hit => hit || Response.error())));
});
