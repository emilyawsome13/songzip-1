const CACHE_NAME = "songzip-static-v16";
const STATIC_ASSETS = [
  "/",
  "/styles.css?v16",
  "/app.js?v16",
  "/manifest.webmanifest?v16",
  "/icon.svg?v16",
  "/privacy.html",
  "/acceptable-use.html",
  "/privacy-policy.docx",
  "/acceptable-use-policy.docx",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS)),
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.map((key) => {
          if (key !== CACHE_NAME) {
            return caches.delete(key);
          }
          return Promise.resolve();
        }),
      ),
    ),
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") {
    return;
  }

  const requestUrl = new URL(event.request.url);
  if (requestUrl.origin !== self.location.origin) {
    return;
  }

  if (event.request.mode === "navigate") {
    event.respondWith(
      fetch(event.request).catch(() =>
        caches.match(event.request).then((match) => match || caches.match("/")),
      ),
    );
    return;
  }

  if (
    requestUrl.pathname.startsWith("/api/")
    || requestUrl.pathname.startsWith("/ws")
  ) {
    return;
  }

  event.respondWith(
    fetch(event.request)
      .then((response) => {
        const responseClone = response.clone();
        event.waitUntil(
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, responseClone)),
        );
        return response;
      })
      .catch(() => caches.match(event.request)),
  );
});
