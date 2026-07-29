/**
 * Mike Service Worker — PWA + Push Notifications
 * ================================================
 * • Caches static assets for offline/installable PWA
 * • Handles Web Push (VAPID) for proactive Mike alerts
 * • Shows native OS notifications via Push API
 */

const CACHE_NAME = "mike-v20260416c";
const STATIC_ASSETS = [
  "/",
  "/install",
  "/static/style.css",
  "/static/app.js?v=20260416c",
  "/static/manifest.webmanifest",
  "/static/icons/mike-icon-192.png",
  "/static/icons/mike-icon-512.png",
];

// ---- Install: pre-cache static shell ----
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) =>
      cache.addAll(STATIC_ASSETS).catch(() => {
        // Non-fatal: some assets may not exist yet
      })
    )
  );
  self.skipWaiting();
});

// ---- Activate: clean up old caches ----
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key !== CACHE_NAME)
            .map((key) => caches.delete(key))
        )
      )
      .then(() => self.clients.claim())
  );
});

// ---- Fetch: prefer network so the web app does not get stuck on stale JS ----
self.addEventListener("fetch", (event) => {
  // Only cache GET same-origin requests
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;
  // Don't cache API calls or SSE streams
  if (url.pathname.startsWith("/v1/") || url.pathname === "/health") return;

  event.respondWith(
    fetch(event.request)
      .then((response) => {
        if (response && response.ok) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
        }
        return response;
      })
      .catch(async () => {
        const cached = await caches.match(event.request);
        if (cached) return cached;
        if (event.request.mode === "navigate") {
          return caches.match("/");
        }
        throw new Error("offline");
      })
  );
});

// ---- Push: show OS notification from proactive Mike alerts ----
self.addEventListener("push", (event) => {
  let data = { title: "Mike", body: "Novo aviso do Mike." };
  try {
    data = event.data ? event.data.json() : data;
  } catch (_) {
    data.body = event.data ? event.data.text() : data.body;
  }
  const options = {
    body: data.body || "",
    icon: "/static/icons/mike-icon-192.png",
    badge: "/static/icons/mike-icon-64.png",
    tag: data.tag || "mike-alert",
    renotify: true,
    data: { url: data.url || "/" },
  };
  event.waitUntil(
    self.registration.showNotification(data.title || "Mike", options)
  );
});

// ---- NotificationClick: focus or open the app ----
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((clients) => {
        for (const client of clients) {
          if (client.url.includes(self.location.origin) && "focus" in client) {
            return client.focus();
          }
        }
        return self.clients.openWindow(target);
      })
  );
});

// ---- Message: receive proactive alerts from open page via postMessage ----
self.addEventListener("message", (event) => {
  if (!event.data || event.data.type !== "MIKE_NOTIFY") return;
  const { title, body, tag } = event.data;
  self.registration.showNotification(title || "Mike", {
    body: body || "",
    icon: "/static/icons/mike-icon-192.png",
    badge: "/static/icons/mike-icon-64.png",
    tag: tag || "mike-msg",
    renotify: true,
  });
});
