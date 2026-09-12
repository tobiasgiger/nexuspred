/* One Server-Sent-Events connection feeds the whole UI. Messages are
   {"kind": event|signal|order|session|discord, "data": {...}}; the server also
   sends a named `ping` every 10 s so liveness is visible even when idle. */
import { store } from "./store.js";

const CAP = 200;
let es = null;
let reconnTimer = null;
let retryTimer = null;
let retryMs = 2000;
let wasDown = false;

function markLive() {
  if (reconnTimer) { clearTimeout(reconnTimer); reconnTimer = null; }
  retryMs = 2000;
  if (store.get("stream") !== "live") store.set("stream", "live");
  if (wasDown) {
    // events emitted while we were away are gone: pull the current picture
    wasDown = false;
    store.set("statusDirty", Date.now());
    store.set("streamResync", Date.now());
  }
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
    wasDown = true;
    if (es && es.readyState === EventSource.CLOSED) {
      // a non-200 answer (502/503 while the bridge restarts) closes the source for
      // good — the browser will not retry that on its own, so we do, with backoff
      es = null;
      store.set("stream", "reconnecting");
      if (!retryTimer) retryTimer = setTimeout(() => { retryTimer = null; connectStream(); }, retryMs);
      retryMs = Math.min(30000, retryMs * 2);
      return;
    }
    // a dropped connection: EventSource reconnects on its own; show "reconnecting" if it stays down
    if (reconnTimer) return;
    reconnTimer = setTimeout(() => { reconnTimer = null; store.set("stream", "reconnecting"); }, 4000);
  };
  es.addEventListener("resync", () => {
    // the server dropped messages for this tab (slow consumer): pull the current picture
    store.set("statusDirty", Date.now());
    store.set("streamResync", Date.now());
  });
  es.onmessage = (m) => {
    markLive();
    let msg;
    try { msg = JSON.parse(m.data); } catch { return; }
    const d = msg.data;
    switch (msg.kind) {
      case "event": prepend("events", d); break;
      case "signal": prepend("signals", d); break;
      case "order": prepend("orders", d); store.set("statusDirty", Date.now()); break;
      case "session": {
        const before = ((store.get("status") || {}).sessions || []).find((s) => s.name === d.name);
        store.update("status", (s) => patchSession(s, d));
        // the health loop emits a session message per check: only a real change re-pulls /api/status
        if (!before || before.connected !== d.connected || (d.environment && before.environment !== d.environment)) store.set("statusDirty", Date.now());
        break;
      }
      case "discord": prepend("discordFeed", d); break;
      case "pnl": store.set("pnl", d); break;
      default: break;
    }
  };
}

