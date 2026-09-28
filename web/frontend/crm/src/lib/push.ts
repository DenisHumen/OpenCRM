import { api } from "./api";

/** Web Push этого браузера: звонок напоминания при закрытой вкладке (docs/bloki/31-web-push.md). */

export interface PushPodpiska {
  id: number;
  nazvanie: string;
  endpoint_hash: string;
  created_at: string | null;
  last_ok_at: string | null;
}

export function pushPodderzhan(): boolean {
  return (
    typeof window !== "undefined" &&
    window.isSecureContext &&
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window
  );
}

function izB64u(stroka: string): Uint8Array<ArrayBuffer> {
  const b64 = stroka.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (stroka.length % 4)) % 4);
  const bayty = atob(b64);
  const itog = new Uint8Array(new ArrayBuffer(bayty.length));
  for (let i = 0; i < bayty.length; i++) itog[i] = bayty.charCodeAt(i);
  return itog;
}

export async function otpechatok(endpoint: string): Promise<string> {
  const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(endpoint));
  return Array.from(new Uint8Array(hash), (b) => b.toString(16).padStart(2, "0")).join("");
}

function nazvanieUstroystva(): string {
  const ua = navigator.userAgent;
  const brauzer = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : "Browser";
  const sistema = /Android/.test(ua) ? "Android" : /iPhone|iPad/.test(ua) ? "iOS" : /Windows/.test(ua) ? "Windows" : /Mac OS X/.test(ua) ? "macOS" : /Linux/.test(ua) ? "Linux" : "";
  return sistema ? `${brauzer} · ${sistema}` : brauzer;
}

/** Подписка этого браузера, если она есть. */
export async function tekushchaya(): Promise<PushSubscription | null> {
  if (!pushPodderzhan()) return null;
  const reg = await navigator.serviceWorker.getRegistration("/");
  return reg ? reg.pushManager.getSubscription() : null;
}

/** Включить на этом устройстве. Разрешение спрашивается здесь — только по нажатию. */
export async function vklyuchitPush(): Promise<"ok" | "denied" | "unsupported"> {
  if (!pushPodderzhan()) return "unsupported";
  if ((await Notification.requestPermission()) !== "granted") return "denied";
  const reg = await navigator.serviceWorker.register("/sw.js");
  await navigator.serviceWorker.ready;
  const { key } = await api.get<{ key: string }>("/push/key");
  let sub = await reg.pushManager.getSubscription();
  // Подписка под прежним ключом сервера (сменили секрет) не примет наших сообщений.
  if (sub) await sub.unsubscribe();
  sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: izB64u(key) });
  const json = sub.toJSON();
  await api.post("/push/subscriptions", { endpoint: json.endpoint, keys: json.keys, nazvanie: nazvanieUstroystva() });
  return "ok";
}

/** Отключить подписку по номеру; если это этот браузер — отписать и его. */
export async function otklyuchitPush(podpiska: PushPodpiska): Promise<void> {
  await api.del(`/push/subscriptions/${podpiska.id}`);
  const sub = await tekushchaya();
  if (sub && (await otpechatok(sub.endpoint)) === podpiska.endpoint_hash) await sub.unsubscribe();
}
