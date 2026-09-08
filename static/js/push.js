/* Web Push client: register the service worker, subscribe this device, tell the bridge. */
import { api } from "./api.js";

const b64ToBytes = (s) => {
  const pad = "=".repeat((4 - (s.length % 4)) % 4);
  const raw = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
};

export const isIOS = () => /iP(hone|ad|od)/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
export const isStandalone = () => window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;

/** Why push can't work here (string), or "" when it can. */
export function unsupportedReason() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
    return isIOS() && !isStandalone()
      ? "On iPhone/iPad, add this app to the Home Screen first (Share → Add to Home Screen) and open it from there — Safari only allows push for installed apps."
      : "This browser does not support Web Push.";
  }
  if (!window.isSecureContext) return "Push needs HTTPS.";
  return "";
}

export async function registerWorker() {
  if (!("serviceWorker" in navigator)) return null;
  try { return await navigator.serviceWorker.register("/sw.js", { scope: "/" }); } catch (e) { return null; }
}

function confirmedSameKey(sub, appKey) {
  // Only TRUE when we can positively confirm the subscription's server key
  // matches. iOS Safari does not expose options.applicationServerKey, so there
  // it returns false and enablePush() recreates the subscription — the only way
  // to be sure a stale key (from a rotated VAPID key) is replaced.
  const stored = sub.options && sub.options.applicationServerKey;
  if (!stored) return false;
  const a = new Uint8Array(stored);
  if (a.length !== appKey.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== appKey[i]) return false;
  return true;
}

export async function currentSubscription() {
  const reg = await registerWorker();
  if (!reg) return null;
  return reg.pushManager.getSubscription();
}

export function deviceName() {
  const ua = navigator.userAgent;
  const os = isIOS() ? (/iPad/.test(ua) || navigator.maxTouchPoints > 1 && navigator.platform === "MacIntel" ? "iPad" : "iPhone")
    : /Android/.test(ua) ? "Android" : /Windows/.test(ua) ? "Windows" : /Mac OS/.test(ua) ? "Mac" : /Linux/.test(ua) ? "Linux" : "Device";
  const browser = /CriOS|Chrome/.test(ua) && !/Edg/.test(ua) ? "Chrome" : /Edg/.test(ua) ? "Edge" : /Firefox|FxiOS/.test(ua) ? "Firefox" : /Safari/.test(ua) ? "Safari" : "Browser";
  return `${os} · ${browser}${isStandalone() ? " (app)" : ""}`;
}

/** Ask permission (must run from a user gesture), subscribe, register with the bridge. */
export async function enablePush() {
  const why = unsupportedReason();
  if (why) throw new Error(why);
  const reg = await registerWorker();
  if (!reg) throw new Error("Service worker registration failed");
  const perm = await Notification.requestPermission();
  if (perm !== "granted") throw new Error("Notification permission was not granted");
  const { public_key } = await api.get("/api/push/public-key");
  const appKey = b64ToBytes(public_key);
  let sub = await reg.pushManager.getSubscription();
  // Reuse the existing subscription only when we can prove it uses the current
  // server key; otherwise drop it and subscribe afresh. On iOS the key is never
  // exposed, so enabling always creates a subscription bound to today's key —
  // which is what recovers a device stuck on a rotated key.
  if (sub && !confirmedSameKey(sub, appKey)) { try { await sub.unsubscribe(); } catch (e) { /* ignore */ } sub = null; }
  if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: appKey });
  const json = sub.toJSON();
  return api.post("/api/push/subscribe", { subscription: json, device: deviceName() });
}

export async function disablePush() {
  const sub = await currentSubscription();
  if (sub) {
    try { await api.del("/api/push/subscribe", { endpoint: sub.endpoint }); } catch (e) { /* ignore */ }
    await sub.unsubscribe();
  }
}
