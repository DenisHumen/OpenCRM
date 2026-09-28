// Service worker OpenCRM: звонок напоминания, когда вкладка закрыта (docs/bloki/31-web-push.md).
// Тег тот же, что у окна вкладки (`lib/signaly.ts`), — система склеит два окна в одно.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (e) => {
  let d = {};
  try {
    d = e.data ? e.data.json() : {};
  } catch {
    d = {};
  }
  const knopki = d.knopki || {};
  e.waitUntil(
    self.registration.showNotification(d.title || "OpenCRM", {
      body: d.body || "",
      tag: d.tag || "opencrm-napom",
      renotify: true,
      icon: "/static/favicon.svg",
      // Срочное висит до ответа: пропавшее само — это пропущенное срочное.
      requireInteraction: !!d.srochno,
      data: { url: d.url || "/tasks", deystvie: d.deystvie || "" },
      actions: d.deystvie
        ? [
            { action: "done", title: knopki.done || "Done" },
            { action: "later", title: knopki.later || "Later" },
          ]
        : [],
    }),
  );
});

self.addEventListener("notificationclick", (e) => {
  const okno = e.notification;
  okno.close();
  const { url, deystvie } = okno.data || {};
  if ((e.action === "done" || e.action === "later") && deystvie) {
    // Без cookie: подпись в токене заменяет вход, а cookie без CSRF-заголовка получила бы отказ.
    e.waitUntil(
      fetch("/api/v1/push/action", {
        method: "POST",
        credentials: "omit",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: deystvie, deystvie: e.action }),
      })
        .then((otvet) => (otvet.ok ? undefined : otkryt(url)))
        .catch(() => otkryt(url)),
    );
    return;
  }
  e.waitUntil(otkryt(url));
});

async function otkryt(url) {
  const okna = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  for (const o of okna) {
    if (new URL(o.url).origin !== self.location.origin) continue;
    await o.focus();
    try {
      await o.navigate(url);
    } catch {
      /* чужая вкладка без контроля — хватит того, что она впереди */
    }
    return;
  }
  await self.clients.openWindow(url);
}
