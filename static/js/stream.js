/* One Server-Sent-Events connection feeds the whole UI. Messages are
   {"kind": event|signal|order|session|discord, "data": {...}}; the server also
   sends a named `ping` every 10 s so liveness is visible even when idle. */
import { store } from "./store.js";

const CAP = 200;
let es = null;
let reconnTimer = null;

function markLive() {
  if (reconnTimer) { clearTimeout(reconnTimer); reconnTimer = null; }
  if (store.get("stream") !== "live") store.set("stream", "live");
}

function prepend(key, item) {
  store.update(key, (list) => [item, ...(list || [])].slice(0, CAP));
}

function patchSession(status, sess) {
  if (!status) return status;
  const sessions = [...(status.sessions || [])];
  const i = sessions.findIndex((s) => s.name === sess.name);
  if (i >= 0) sessions[i] = { ...sessions[i], ...sess }; else sessions.push(sess);
  const connected = sessions.filter((s) => s.connected).length;
  return { ...status, sessions,
    connection: { connected: connected > 0, accounts_total: sessions.length, accounts_connected: connected } };
}

export function connectStream() {
  if (es) es.close();
  try {
    es = new EventSource("/api/stream");
  } catch (e) {
    store.set("stream", "off");
    return;
  }
  es.onopen = markLive;
  es.addEventListener("ping", markLive);
  es.onerror = () => {
    // EventSource reconnects on its own; only show "reconnecting" if it stays down.
    if (reconnTimer) return;
    reconnTimer = setTimeout(() => { reconnTimer = null; store.set("stream", "reconnecting"); }, 4000);
  };
  es.onmessage = (m) => {
    markLive();
    let msg;
    try { msg = JSON.parse(m.data); } catch { return; }
    const d = msg.data;
    switch (msg.kind) {
      case "event": prepend("events", d); break;
      case "signal": prepend("signals", d); break;
      case "order": prepend("orders", d); store.set("statusDirty", Date.now()); break;
      case "session": store.update("status", (s) => patchSession(s, d)); store.set("statusDirty", Date.now()); break;
      case "discord": prepend("discordFeed", d); break;
      case "pnl": store.set("pnl", d); break;
      default: break;
    }
  };
}

export function disconnectStream() {
  if (es) es.close();
  es = null;
  store.set("stream", "off");
}
