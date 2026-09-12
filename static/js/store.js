/* Minimal reactive store: pages subscribe to keys and re-render on change. */

export function createStore(initial = {}) {
  const state = { ...initial };
  const subs = new Map();

  function emit(key) {
    for (const fn of subs.get(key) || []) {
      try { fn(state[key], key); } catch (e) { console.error(`store subscriber for "${key}" failed`, e); }
    }
  }

  return {
    get: (key) => state[key],
    set(key, value) {
      state[key] = value;
      emit(key);
    },
    update(key, fn) {
      this.set(key, fn(state[key]));
    },
    /** subscribe("a" | ["a","b"], fn, {immediate}) → unsubscribe() */
    subscribe(keys, fn, { immediate = false } = {}) {
      const list = Array.isArray(keys) ? keys : [keys];
      for (const k of list) {
        if (!subs.has(k)) subs.set(k, new Set());
        subs.get(k).add(fn);
      }
      if (immediate) for (const k of list) fn(state[k], k);
      return () => { for (const k of list) subs.get(k)?.delete(fn); };
    },
  };
}

export const store = createStore({
  me: null,            // {id, email, is_admin, features}
  status: null,        // /api/status
  pnl: null,           // live account P&L (from /api/status, then the stream)
  settings: null,      // /api/settings (secrets masked)
  webhooks: [],
  tradeAccounts: [],
  tokenAccounts: [],
  orders: [],
  events: [],
  signals: [],
  positions: null,     // null = not loaded yet
  exposure: null,      // /api/exposure summary (with the positions)
  discordStatus: null,
  discordFeed: [],
  discordConfig: null,
  update: null,        // /api/update/check
  stream: "off",       // off | live | reconnecting
  route: null,         // {path, params, query}
  statusDirty: 0,      // bumped by the stream when a refetch of /api/status is due
});

/** Feature / role gates used by navigation and pages. */
export function can(me, what) {
  if (!me) return false;
  if (what === "admin") return !!me.is_admin;
  if (what === "discord") return (me.features || {}).discord_signals !== false;
  return true;
}
