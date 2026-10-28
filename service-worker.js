/* New cache version forces clients off the old three-photo slideshow assets. */
const CACHE = "kets-display-v19-eric-investments-six-photo-20261028";
const APP_SHELL = [
  "/",
  "/index.html",
  "/styles.css?v=20261028sixphotodisplayfix",
  "/app.js?v=20261024devicefix",
  "/manifest.json",
  "/icon-192.png",
  "/icon-512.png",
  "/background-slideshow.js?v=20261028sixphotodisplayfix",
  "/eric-investments-background.png?v=20261028slide1",
  "/executive-car-background.jpg?v=20261028slide2",
  "/executive-boardroom-background.jpg?v=20261028slide3",
  "/eric-investments-gold-background.jpg?v=20261028slide4",
  "/eric-investments-diamonds-background.jpg?v=20261028slide5",
  "/eric-investments-shares-presentation-background.jpg?v=20261028slide6"
];

self.addEventListener("install", event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    await Promise.all(APP_SHELL.map(async url => {
      try {
        const response = await fetch(url, { cache: "reload" });
        if (response.ok) await cache.put(url, response);
      } catch (_) {}
    }));
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  if (event.request.method !== "GET" || !event.request.url.startsWith(self.location.origin)) return;
  event.respondWith((async () => {
    try {
      const response = await fetch(event.request, { cache: "no-store" });
      if (response.ok) {
        const cache = await caches.open(CACHE);
        cache.put(event.request, response.clone()).catch(() => {});
      }
      return response;
    } catch (_) {
      return (await caches.match(event.request)) || (await caches.match("/")) || Response.error();
    }
  })());
});
