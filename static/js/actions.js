/* Data actions: every API call the pages share, writing into the store. */
import { api } from "./api.js";
import { store, can } from "./store.js";
import { toast, mergeLive } from "./ui.js";
import { setPublicOrigin } from "./templates.js";
import { t, adopt as adoptLanguage, lang as currentLanguage } from "./i18n.js";

const quiet = async (fn) => { try { return await fn(); } catch (e) { return undefined; } };

export const actions = {
  async loadMe() {
    const me = await api.get("/api/me");
    store.set("me", me);
    return me;
  },
  async loadSettings() {
    const s = await api.get("/api/settings");
    store.set("settings", s);
    if (adoptLanguage(s.ui_language)) window.location.reload();   // the workspace's language differs from the cached one
    // alerts (Discord / e-mail / push) follow the dashboard's language: tell the server once what it resolved
    if (s.ui_language_seen !== currentLanguage()) quiet(async () => { store.set("settings", await api.post("/api/settings", { ui_language_seen: currentLanguage() })); });
    return s;
  },
  async saveSettings(updates) {
    const s = await api.post("/api/settings", updates);
    store.set("settings", s);
    quiet(() => actions.refreshStatus());
    if ("ui_language" in updates && adoptLanguage(s.ui_language)) setTimeout(() => window.location.reload(), 150);
    return s;
  },
  refreshStatus: () => quiet(async () => {
    const s = await api.get("/api/status");
    setPublicOrigin(s.public_url);
    const cur = store.get("pnl");
    if (s.pnl && s.pnl.ts && !(cur && cur.ts && cur.ts > s.pnl.ts)) store.set("pnl", s.pnl);   // never overwrite a newer stream snapshot
    store.set("status", s);
    return s;
  }),
  refreshOrders: () => quiet(async () => store.set("orders", mergeLive(store.get("orders"), await api.get("/api/orders")))),
  checkRollover: () => quiet(async () => { await api.post("/api/rollover/check"); return actions.refreshStatus(); }),
  refreshLogs: () => quiet(async () => {
    const [events, signals] = await Promise.all([api.get("/api/events"), api.get("/api/signals")]);
    // frames the stream delivered meanwhile stay on top; unchanged rows keep their identity
    store.set("events", mergeLive(store.get("events"), events));
    store.set("signals", mergeLive(store.get("signals"), signals));
  }),
  async refreshPositions() {
    try {
      const x = await api.get("/api/exposure");        // one broker poll: the rows and what they add up to
      store.set("positions", x.positions || []);
      store.set("exposure", x);
    } catch (e) {
      store.set("positions", { error: e.message });
      store.set("exposure", null);
    }
  },
  loadWebhooks: () => quiet(async () => { const w = await api.get("/api/webhooks"); store.set("webhooks", w); return w; }),
  loadTradeAccounts: () => quiet(async () => { const a = await api.get("/api/trade-accounts"); store.set("tradeAccounts", a); return a; }),
  loadTokenAccounts: () => quiet(async () => { const a = await api.get("/api/token-accounts"); store.set("tokenAccounts", a); return a; }),
  async connectAll() {
    toast(t("Connecting…"));
    try {
      const r = await api.post("/api/connect");
      const sessions = r.sessions || [];
      const ok = sessions.filter((x) => x.connected).length;
      toast(t("Connected {ok}/{total} account(s)", { ok, total: sessions.length }), ok ? "success" : "error");
      await Promise.all([actions.loadTradeAccounts(), actions.loadTokenAccounts(), actions.refreshStatus()]);
      return r;
    } catch (e) {
      toast(t("Connect failed: {error}", { error: e.message }), "error");
      return null;
    }
  },
  async healthCheck() {
    toast(t("Checking connections…"));
    try {
      const r = await api.get("/api/health");
      const sessions = r.sessions || [];
      const ok = sessions.filter((x) => x.connected).length;
      toast(t("{ok}/{total} account(s) healthy", { ok, total: sessions.length }), ok ? "success" : "error");
      actions.refreshStatus();
    } catch (e) { toast(e.message, "error"); }
  },
  checkUpdate: () => quiet(async () => { const u = await api.get("/api/update/check"); store.set("update", u); return u; }),
  refreshDiscordStatus: () => quiet(async () => {
    if (!can(store.get("me"), "discord")) return null;
    const s = await api.get("/api/discord/status");
    store.set("discordStatus", s);
    return s;
  }),
  loadDiscordFeed: () => quiet(async () => {
    if (!can(store.get("me"), "discord")) return;
    store.set("discordFeed", await api.get("/api/discord/signals"));
  }),
  loadDiscordConfig: () => quiet(async () => {
    if (!can(store.get("me"), "discord")) return null;
    const c = await api.get("/api/discord/config");
    store.set("discordConfig", c);
    return c;
  }),
  async setTrading(enabled) {
    await actions.saveSettings({ trading_enabled: !!enabled });
    toast(enabled ? t("Trading ENABLED") : t("Trading disabled"), enabled ? "warn" : "success");
  },
  async flattenAll() {
    const r = await api.post("/api/flatten-all");
    actions.refreshStatus();
    actions.refreshOrders();
    actions.refreshLogs();
    actions.refreshPositions();
    return r;
  },
};
