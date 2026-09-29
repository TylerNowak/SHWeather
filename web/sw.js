// Service worker: the app shell is cached so the page opens even when the phone cannot
// reach the Pi; API reads are network-first with the last good answer as fallback.
// Requires a secure context (HTTPS or localhost); see docs/DEPLOYMENT.md.

const VERSION = "shw-0.4.0";
const SHELL = [
  "./", "index.html", "css/app.css", "manifest.webmanifest",
  "js/app.js", "js/api.js", "js/charts.js", "js/daily.js", "js/dom.js", "js/forecast.js", "js/radar.js", "js/settings.js",
  "js/text.js", "js/units.js",
  "data/basemap.json",
  "icons/icon.svg", "icons/icon-192.png", "icons/icon-512.png", "icons/apple-touch-icon.png",
];
const API_CACHE = `${VERSION}-api`;
const API_TIMEOUT_MS = 6000;

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => !k.startsWith(VERSION)).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

async function networkFirst(request) {
  const cache = await caches.open(API_CACHE);
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), API_TIMEOUT_MS);
    const res = await fetch(request, { signal: ctrl.signal });
    clearTimeout(timer);
    if (res.ok) cache.put(request, res.clone());
    return res;
  } catch (err) {
    const hit = await cache.match(request);
    if (!hit) throw err;
    const headers = new Headers(hit.headers);
    headers.set("X-SHW-Cached", "1");
    return new Response(await hit.blob(), { status: hit.status, headers });
  }
}

async function cacheFirst(request) {
  const hit = await caches.match(request);
  const refresh = fetch(request).then((res) => {
    if (res.ok) caches.open(VERSION).then((c) => c.put(request, res.clone()));
    return res;
  }).catch(() => hit);
  return hit || refresh;
}

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || event.request.method !== "GET") return;
  // Radar tiles: many, short-lived and cached by the server; the browser's HTTP cache is enough.
  // The frame list may take longer than the API timeout below (the server is downloading).
  if (url.pathname.includes("/api/tiles/") || url.pathname.endsWith("/api/imagery")) return;
  if (url.pathname.includes("/api/")) event.respondWith(networkFirst(event.request));
  else if (!url.pathname.endsWith("/docs") && !url.pathname.endsWith("openapi.json")) event.respondWith(cacheFirst(event.request));
});
