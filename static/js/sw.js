/* Fluxbridge service worker — push notifications only.
   Deliberately no fetch handler / caching: every deploy must load fresh modules. */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = { title: "Fluxbridge", body: event.data ? event.data.text() : "" }; }
  const title = data.title || "Fluxbridge";
  const options = {
    body: data.body || "",
    icon: "/static/icons/icon-192.png",
    badge: "/static/icons/icon-192.png",
    tag: data.tag || "fluxbridge",
    renotify: true,
    data: { url: data.url || "/" },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of all) {
      if ("focus" in c) { try { await c.navigate(url); } catch (e) { /* ignore */ } return c.focus(); }
    }
    return self.clients.openWindow(url);
  })());
});
