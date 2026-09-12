/* Marketplace: signals other users published; subscribe with your own accounts. */
import { h, card, tag, toast, confirmDialog, pageHead, clear } from "../ui.js";
import { maskAccount } from "../privacy.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { store } from "../store.js";
import { actions } from "../actions.js";
import { dataTable } from "../components/table.js";
import { openDrawer, closeDrawer } from "../components/drawer.js";
import { accountKey, routedAccountsTable } from "../components/accounts.js";
import { STRATEGY_LABEL } from "../templates.js";
import { sizingOf } from "../sizing.js";
import { lineChart, fmtSigned } from "../charts.js";
import { tradeWindowEditor } from "../components/tradeWindow.js";
import { recordStrip } from "./subscriptions.js";
import { t } from "../i18n.js";


/**
 * Subscribe / edit-subscription drawer.
 * item: {publisher_area_id, webhook_id, title, description, strategy, publisher_email, subscription|null}
 * onDone(): called after save / unsubscribe.
 */
export function openSubscriptionDrawer(item, onDone) {
  const sub = item.subscription || null;
  const known = store.get("tradeAccounts") || [];
  const selected = new Map(((sub && sub.accounts) || []).map((a) => [accountKey(a.token_idx, a.spec), a]));
  const enabledSw = h("input", { type: "checkbox", class: "switch", checked: sub ? !!sub.enabled : true });
  const accTable = routedAccountsTable({ known, selected });
  const collect = accTable.collect;
  // --- subscriber controls (alpha.78)
  const c = { symbols: [], trade_window: null, max_qty: 0, max_signals_per_day: 0, pause_after_errors: 0, ...((sub && sub.controls) || {}) };
  const symIn = h("input", { type: "text", value: (c.symbols || []).join(", "), placeholder: t("empty = every symbol the publisher sends"), autocomplete: "off" });
  const maxQty = h("input", { type: "number", min: 0, max: 1000, step: 1, value: c.max_qty || 0, style: "max-width:140px" });
  const maxDay = h("input", { type: "number", min: 0, max: 500, step: 1, value: c.max_signals_per_day || 0, style: "max-width:140px" });
  const pauseN = h("input", { type: "number", min: 0, max: 50, step: 1, value: c.pause_after_errors || 0, style: "max-width:140px" });
  const win = tradeWindowEditor(c.trade_window, { hint: t("Entries from this publisher only run inside this window. Closes always run.") });
  const controlsBlock = h("div", null,
    h("h3", null, t("My limits for this signal")),
    h("p", { class: "hint" }, t("Your own guard rails on top of the publisher's signal — they never change what the publisher sends.")),
    h("div", { class: "grid grid-2" },
      h("div", { class: "field" }, h("label", null, t("Only these symbols (roots)")), symIn),
      h("div", { class: "field" }, h("label", null, t("Max contracts per signal and account")), maxQty, h("div", { class: "field-hint" }, t("0 = no cap. Caps the sizing above."))),
      h("div", { class: "field" }, h("label", null, t("Max entries per day")), maxDay, h("div", { class: "field-hint" }, t("0 = unlimited. Counted per UTC day; closes are never blocked."))),
      h("div", { class: "field" }, h("label", null, t("Switch off after consecutive errors")), pauseN, h("div", { class: "field-hint" }, t("0 = never. The subscription turns itself off and you get an alert.")))),
    win.el);
  const collectControls = () => ({ symbols: symIn.value.split(",").map((x) => x.trim()).filter(Boolean), max_qty: Number(maxQty.value) || 0,
    max_signals_per_day: Number(maxDay.value) || 0, pause_after_errors: Number(pauseN.value) || 0, trade_window: win.collect() });

  const saveBtn = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
    const body = { enabled: enabledSw.checked, accounts: collect(), controls: collectControls() };
    if (!body.accounts.length && enabledSw.checked) {
      if (!(await confirmDialog({ title: t("No accounts routed"), body: t("The subscription will be active but trade on no account. Save anyway?"), confirmText: t("Save") }))) return;
    }
    saveBtn.disabled = true;
    try {
      if (sub) await api.put(`/api/subscriptions/${sub.id}`, body);
      else await api.post(`/api/marketplace/${item.publisher_area_id}/${item.webhook_id}/subscribe`, body);
      toast(sub ? t("Subscription saved") : t("Subscribed to {title}", { title: item.title }), "success");
      closeDrawer();
      if (onDone) onDone();
    } catch (e) { toast(e.message, "error"); } finally { saveBtn.disabled = false; }
  } }, icon("check"), sub ? t("Save") : t("Subscribe"));
  const unsubBtn = sub ? h("button", { type: "button", class: "btn btn-danger", onClick: async () => {
    if (!(await confirmDialog({ title: t("Unsubscribe from \"{title}\"?", { title: item.title }), body: t("Future signals from this publisher won't reach your accounts. Open positions are not touched."), confirmText: t("Unsubscribe"), danger: true }))) return;
    try { await api.del(`/api/subscriptions/${sub.id}`); toast(t("Unsubscribed"), "success"); closeDrawer(); if (onDone) onDone(); }
    catch (e) { toast(e.message, "error"); }
  } }, icon("trash"), t("Unsubscribe")) : null;

  openDrawer({
    title: item.title,
    body: [
      h("div", { class: "callout" },
        h("div", null, h("strong", null, t("Publisher: ")), item.publisher_email || "—", " · ", h("strong", null, t("Strategy: ")), tag(STRATEGY_LABEL[item.strategy] || item.strategy, item.strategy)),
        item.description ? h("div", { style: "margin-top:6px;white-space:pre-line" }, item.description) : null),
      h("label", { class: "switch-row" }, h("span", null, t("Subscription active"), h("small", null, t("Off = signals from this publisher are ignored for your accounts. Your own Trading switch applies as well."))), enabledSw),
      h("h3", null, t("Trade on my accounts")),
      h("p", { class: "hint" }, t("Signals execute on every routed account below, in parallel, sized per account (Same 1:1, Multiplier, or Fixed contracts with an optional Max). The publisher never sees your accounts.")),
      accTable.el,
      controlsBlock,
    ],
    foot: [saveBtn, h("button", { type: "button", class: "btn btn-ghost", onClick: () => closeDrawer() }, t("Close")), h("span", { style: "flex:1" }), unsubBtn],
  });
}

/**
 * Follow a published copy group with your own accounts (copy sizing rules).
 * item: {publisher_area_id, group_id, title, description, symbols, environment, publisher_email, subscription|null}
 */
export function openCopySubscriptionDrawer(item, onDone) {
  const sub = item.subscription || null;
  const known = store.get("tradeAccounts") || [];
  const selected = new Map(((sub && sub.accounts) || []).map((a) => [String(a.spec), a]));
  const enabledSw = h("input", { type: "checkbox", class: "switch", checked: sub ? !!sub.enabled : true });
  const q = (cls, spec) => accTable.tbody.querySelector(`.${cls}[data-spec="${CSS.escape(spec)}"]`);
  const accTable = dataTable({
    compact: true,
    empty: t("No trade accounts discovered yet — add a login under Settings → Broker Accounts and Connect & Verify."),
    columns: [
      { label: t("Follow"), render: (a) => h("input", { type: "checkbox", class: "switch cs-on", checked: selected.has(String(a.spec)) && (selected.get(String(a.spec)).enabled !== false), dataset: { spec: a.spec } }) },
      { label: t("Account"), render: (a) => h("span", null, h("code", null, maskAccount(a.spec)), h("small", { class: "muted", style: "display:block" }, `${a.token_name} · ${(a.environment || "").toUpperCase()}`)) },
      { label: t("Mode"), render: (a) => { const f = selected.get(String(a.spec)) || {}; return h("select", { class: "cs-mode input-sm", dataset: { spec: a.spec } },
        h("option", { value: "multiplier", selected: (f.mode || "multiplier") === "multiplier" }, t("Multiplier")), h("option", { value: "fixed", selected: f.mode === "fixed" }, t("Fixed"))); } },
      { label: "×", render: (a) => h("input", { type: "number", class: "cs-mult input-sm", min: 0.01, step: 0.01, style: "width:70px", value: (selected.get(String(a.spec)) || {}).multiplier ?? 1, dataset: { spec: a.spec } }) },
      { label: t("Fixed"), render: (a) => h("input", { type: "number", class: "cs-fixed input-sm", min: 1, step: 1, style: "width:64px", value: (selected.get(String(a.spec)) || {}).fixed ?? 1, dataset: { spec: a.spec } }) },
      { label: t("Max"), render: (a) => h("input", { type: "number", class: "cs-max input-sm", min: 0, step: 1, style: "width:64px", title: t("0 = no cap"), value: (selected.get(String(a.spec)) || {}).max_contracts ?? 0, dataset: { spec: a.spec } }) },
      { label: t("Direction"), render: (a) => { const f = selected.get(String(a.spec)) || {}; return h("select", { class: "cs-dir input-sm", dataset: { spec: a.spec } },
        ["both", "long", "short"].map((d) => h("option", { value: d, selected: (f.direction || "both") === d }, d))); } },
    ],
  });
  accTable.update(known);
  const collect = () => known.map((a) => {
    const on = q("cs-on", a.spec);
    if (!on || !on.checked) return null;
    return { spec: a.spec, lid: a.lid || "", token_idx: a.token_idx, account_id: a.id, enabled: true,
      mode: q("cs-mode", a.spec).value, multiplier: Number(q("cs-mult", a.spec).value) || 1, fixed: Number(q("cs-fixed", a.spec).value) || 1,
      max_contracts: Number(q("cs-max", a.spec).value) || 0, direction: q("cs-dir", a.spec).value };
  }).filter(Boolean);
  const saveBtn = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
    const body = { enabled: enabledSw.checked, accounts: collect() };
    if (!body.accounts.length) return toast(t("Switch on at least one account"), "error");
    saveBtn.disabled = true;
    try {
      if (sub) await api.put(`/api/subscriptions/${sub.id}`, body);
      else await api.post(`/api/marketplace/${item.publisher_area_id}/copy/${item.group_id}/subscribe`, body);
      toast(sub ? t("Copy subscription saved") : t("Following {title}", { title: item.title }), "success");
      closeDrawer();
      if (onDone) onDone();
    } catch (e) { toast(e.message, "error"); } finally { saveBtn.disabled = false; }
  } }, icon("check"), sub ? t("Save") : t("Follow"));
  const unsubBtn = sub ? h("button", { type: "button", class: "btn btn-danger", onClick: async () => {
    if (!(await confirmDialog({ title: t("Stop following \"{title}\"?", { title: item.title }), body: t("Your accounts leave the mirror. Positions they hold are NOT closed — flatten them yourself if you want to be flat."), confirmText: t("Stop following"), danger: true }))) return;
    try { await api.del(`/api/subscriptions/${sub.id}`); toast(t("Stopped following"), "success"); closeDrawer(); if (onDone) onDone(); }
    catch (e) { toast(e.message, "error"); }
  } }, icon("trash"), t("Stop following")) : null;
  openDrawer({
    title: item.title,
    width: "760px",
    body: [
      h("div", { class: "callout" },
        h("div", null, h("strong", null, t("Leader: ")), item.publisher_email || "—", " · ", tag("copy trading", "accent"), " ", tag((item.environment || "demo").toUpperCase(), item.environment === "live" ? "live" : "demo"),
          (item.symbols || []).length ? [" · ", h("strong", null, t("Symbols: ")), item.symbols.join(", ")] : [" · ", h("span", { class: "muted" }, t("every contract the leader trades"))]),
        item.description ? h("div", { style: "margin-top:6px;white-space:pre-line" }, item.description) : null),
      h("label", { class: "switch-row" }, h("span", null, t("Subscription active"), h("small", null, t("Off = your accounts leave the mirror (positions stay). Your own Trading switch and risk locks apply as well."))), enabledSw),
      h("h3", null, t("Follow with my accounts")),
      h("p", { class: "hint" }, t("Every position change of the leader is mirrored onto the accounts below, live, at market. Multiplier: leader size × factor. Fixed: this many contracts per leader entry. Max caps the size; Direction copies only longs or only shorts. A follower account is exclusive: do not trade it by hand or through another route. The leader never sees your accounts.")),
      accTable.el,
      h("div", { class: "callout warn", style: "margin-top:10px" }, t("Positions the leader already holds when you start following are not copied (baseline). Mirroring of such a contract begins once the leader is flat again. If the leader's feed is lost, the group's feed-loss rule applies to your accounts too (flatten or pause).")),
    ],
    foot: [saveBtn, h("button", { type: "button", class: "btn btn-ghost", onClick: () => closeDrawer() }, t("Close")), h("span", { style: "flex:1" }), unsubBtn],
  });
}

/** Full track record drawer: figures, monthly table, equity curve. */
export async function openRecordDrawer(item) {
  const isCopy = item.kind === "copy";
  let rec;
  try {
    rec = await api.get(isCopy ? `/api/marketplace/${item.publisher_area_id}/copy/${item.group_id}/record` : `/api/marketplace/${item.publisher_area_id}/${item.webhook_id}/record`);
  } catch (e) { toast(e.message, "error"); return; }
  const kv = (k, v) => h("div", { class: "tr-cell" }, h("div", { class: "k" }, k), h("div", { class: "v" }, v));
  const money = (v) => h("span", { class: `pnl ${Number(v) > 0 ? "pos" : Number(v) < 0 ? "neg" : ""}` }, fmtSigned(v, 2));
  const pct = (v) => `${Math.round((v || 0) * 100)}%`;
  const months = dataTable({ compact: true, empty: t("No closed trades yet."), columns: [
    { label: t("Month"), render: (m) => m.bucket },
    { label: t("Trades"), className: "num", render: (m) => String(m.trades) },
    { label: t("Win rate"), className: "num", render: (m) => pct(m.win_rate) },
    { label: t("Net"), className: "num", render: (m) => money(m.net_pnl) },
    { label: t("Cumulative"), className: "num", render: (m) => money(m.cumulative) },
  ] });
  months.update(rec.monthly || []);
  const symbols = dataTable({ compact: true, empty: t("—"), columns: [
    { label: t("Symbol"), render: (b) => b.root },
    { label: t("Trades"), className: "num", render: (b) => String(b.trades) },
    { label: t("Win rate"), className: "num", render: (b) => pct(b.win_rate) },
    { label: t("Net"), className: "num", render: (b) => money(b.net_pnl) },
  ] });
  symbols.update(rec.by_symbol || []);
  const chart = h("div", { class: "chart-box" });
  if ((rec.equity || []).length > 1) chart.append(lineChart(rec.equity.map((p) => ({ label: p.ts.slice(0, 10), value: p.equity })), { height: 180 }));
  const sig = rec.signals;
  openDrawer({
    title: t("Track record — {title}", { title: rec.title || item.title }),
    width: "720px",
    body: [
      h("div", { class: "callout" },
        rec.basis === "none" ? t("This item routes to no account yet, so there is nothing to verify.")
          : rec.verified ? [icon("shield"), " ", t("Every trade below was paired from broker fills the bridge imported itself — nothing here was typed in by the publisher.")]
          : t("{p} of the trades come from broker fills the bridge imported; the rest were uploaded as CSV by the publisher.", { p: pct(rec.verified_share) }),
        " ", isCopy ? t("Basis: the leader account's journal.") : t("Basis: the publisher's journal on the {n} account(s) this signal routes to — it includes anything else those accounts traded.", { n: rec.accounts_n })),
      h("div", { class: "tr-grid" },
        kv(t("Trades"), String(rec.trades)), kv(t("Win rate"), pct(rec.win_rate)), kv(t("Profit factor"), rec.profit_factor == null ? "∞" : String(rec.profit_factor)),
        kv(t("Net P&L"), money(rec.net_pnl)), kv(t("Last 30 days"), money(rec.net_30d)), kv(t("Last 90 days"), money(rec.net_90d)),
        kv(t("Max drawdown"), money(rec.max_drawdown)), kv(t("Expectancy / trade"), money(rec.expectancy)), kv(t("Trading days"), String(rec.trading_days)),
        kv(t("Avg win / loss"), `${fmtSigned(rec.avg_win, 0)} / ${fmtSigned(rec.avg_loss, 0)}`), kv(t("Largest win / loss"), `${fmtSigned(rec.largest_win, 0)} / ${fmtSigned(rec.largest_loss, 0)}`),
        kv(t("Streaks (W / L)"), `${rec.longest_win_streak} / ${rec.longest_loss_streak}`),
        sig ? kv(t("Signals (all / 30 d)"), `${sig.executed} / ${(rec.signals_30d || {}).executed || 0}`) : null,
        rec.latency ? kv(t("Execution latency p50 / p95"), `${rec.latency.p50} / ${rec.latency.p95} ms`) : null,
        kv(t("First / last trade"), `${rec.first_trade_at ? rec.first_trade_at.slice(0, 10) : "—"} → ${rec.last_trade_at ? rec.last_trade_at.slice(0, 10) : "—"}`)),
      h("h3", null, t("Equity curve")), chart,
      h("h3", null, t("By month")), months.el,
      h("h3", null, t("By symbol")), symbols.el,
      h("p", { class: "hint" }, t("Past results are no promise of future ones. Sizing, fees and slippage differ per account.")),
    ],
    foot: [h("button", { type: "button", class: "btn btn-ghost", onClick: () => closeDrawer() }, t("Close"))],
  });
}

export default {
  title: t("Marketplace"),
  render(root, { navigate }) {
    const grid = h("div", { class: "mk-grid" });
    const status = h("p", { class: "hint" }, t("Loading…"));
    // --- discovery: search, sort, filters (client-side over the listing)
    let all = [];
    const filters = { q: "", sort: "net_30d", kind: "", verified: false, tag: "" };
    const qIn = h("input", { type: "search", class: "input-sm", placeholder: t("Search title, publisher, tags…"), style: "min-width:220px", onInput: (e) => { filters.q = e.target.value.trim().toLowerCase(); paint(); } });
    const sortSel = h("select", { class: "input-sm", onChange: (e) => { filters.sort = e.target.value; paint(); } },
      [["net_30d", t("Best last 30 days")], ["net_pnl", t("Best net P&L")], ["win_rate", t("Highest win rate")], ["trades", t("Most trades")], ["subscribers", t("Most subscribers")], ["newest", t("Newest")]]
        .map(([v, l]) => h("option", { value: v }, l)));
    const kindSel = h("select", { class: "input-sm", onChange: (e) => { filters.kind = e.target.value; paint(); } },
      [["", t("Signals + copy")], ["webhook", t("Signals only")], ["copy", t("Copy trading only")]].map(([v, l]) => h("option", { value: v }, l)));
    const verifiedBox = h("input", { type: "checkbox", onChange: (e) => { filters.verified = e.target.checked; paint(); } });
    const bar = h("div", { style: "display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:12px" }, qIn, sortSel, kindSel,
      h("label", { class: "check-row", style: "padding:0;border:0" }, verifiedBox, " ", t("broker-verified only")));
    const recOf = (it) => it.record || {};
    const matches = (it) => {
      if (filters.kind && it.kind !== filters.kind) return false;
      if (filters.verified && !recOf(it).verified) return false;
      if (filters.tag && !(it.tags || []).includes(filters.tag)) return false;
      if (filters.q) {
        const hay = `${it.title} ${it.description || ""} ${it.publisher_email || ""} ${(it.tags || []).join(" ")} ${it.strategy || ""}`.toLowerCase();
        if (!hay.includes(filters.q)) return false;
      }
      return true;
    };
    const rank = (it) => {
      const r = recOf(it);
      switch (filters.sort) {
        case "net_pnl": return Number(r.net_pnl) || 0;
        case "win_rate": return r.trades ? Number(r.win_rate) || 0 : -1;
        case "trades": return Number(r.trades) || 0;
        case "subscribers": return Number(it.subscriber_count) || 0;
        case "newest": return it.published_at || "";
        default: return r.trades ? Number(r.net_30d) || 0 : -Infinity;
      }
    };

    function paint() {
        clear(grid);
        const items = all.filter(matches).sort((a, b) => (rank(b) > rank(a) ? 1 : rank(b) < rank(a) ? -1 : String(a.title).localeCompare(String(b.title))));
        if (!items.length) {
          grid.append(h("div", { class: "card", style: "grid-column:1/-1" }, h("div", { class: "empty-state" }, icon("store"), h("div", null, all.length ? t("Nothing matches your search.") : t("No signals are published for your account right now.")))));
          return;
        }
        for (const it of items) {
          const sub = it.subscription;
          const isCopy = it.kind === "copy";
          const live = isCopy ? (!it.enabled ? tag("group off", "warn") : it.paused ? tag("paused", "warn") : it.running && it.feed_ok ? tag("live", "on") : it.running ? tag("feed lost", "off") : tag("starting", "")) : null;
          const full = it.max_subscribers && it.subscriber_count >= it.max_subscribers && !sub;
          const state = !sub ? (full ? tag(t("full"), "warn") : tag("not subscribed")) : sub.status === "pending" ? tag(t("awaiting approval"), "warn") : sub.status === "paused" ? tag(t("paused by publisher"), "warn")
            : (isCopy ? !it.enabled : !it.webhook_enabled) ? tag("paused by publisher", "warn") : sub.enabled ? tag(isCopy ? t("following · on") : t("subscribed · on"), "on") : tag(isCopy ? t("following · off") : t("subscribed · off"), "off");
          const open = () => (isCopy ? openCopySubscriptionDrawer(it, load) : openSubscriptionDrawer(it, load));
          grid.append(h("div", { class: "card mk-card" },
            h("div", { class: "mk-title" }, h("strong", null, it.title), isCopy ? tag("copy trading", "accent") : tag(STRATEGY_LABEL[it.strategy] || it.strategy, it.strategy),
              isCopy ? tag((it.environment || "demo").toUpperCase(), it.environment === "live" ? "live" : "demo") : null),
            h("div", { class: "mk-desc" }, it.description || (isCopy ? `Mirrors the leader's positions live${(it.symbols || []).length ? ` (${it.symbols.join(", ")})` : ""}.` : "No description.")),
            h("div", { class: "mk-meta" }, icon("user"), it.publisher_email || "—", "·", icon("users"), `${it.subscriber_count}${it.max_subscribers ? `/${it.max_subscribers}` : ""} ${isCopy ? "follower" : "subscriber"}${it.subscriber_count === 1 ? "" : "s"}`,
              live ? ["·", live] : null,
              it.visibility === "selected" ? ["·", tag("invite-only", "accent")] : null,
              it.approval ? ["·", tag(t("approval"), "")] : null,
              (isCopy ? it.publisher_paused : it.paused) ? ["·", tag(t("paused"), "warn")] : null,
              it.published_at ? ["·", t("since {when}", { when: it.published_at.slice(0, 10) })] : null),
            (it.tags || []).length ? h("div", { class: "chips" }, it.tags.map((tg) => h("span", { class: `chip ${filters.tag === tg ? "active" : ""}`, onClick: () => { filters.tag = filters.tag === tg ? "" : tg; paint(); } }, tg))) : null,
            h("div", { class: "mk-record" }, recordStrip(it.record, { compact: true }),
              h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: () => openRecordDrawer(it) }, icon("activity"), t("Track record"))),
            h("div", { class: "mk-foot" }, state,
              h("div", { class: "inline-actions" },
                sub ? h("input", { type: "checkbox", class: "switch", checked: !!sub.enabled, title: t("Enable / disable"), onChange: async (e) => {
                  try { await api.put(`/api/subscriptions/${sub.id}`, { enabled: e.target.checked }); toast(e.target.checked ? t("Subscription enabled") : t("Subscription disabled"), "success"); load(); }
                  catch (err) { e.target.checked = !e.target.checked; toast(err.message, "error"); }
                } }) : null,
                h("button", { type: "button", class: `btn btn-sm ${sub ? "" : "btn-primary"}`, disabled: !!full, onClick: open }, sub ? t("Manage") : isCopy ? t("Follow") : t("Subscribe"))))));
        }
    }
    async function load() {
      try {
        all = await api.get("/api/marketplace");
        status.textContent = "";
        paint();
      } catch (e) {
        status.textContent = e.message;
      }
    }

    root.append(
      pageHead(t("Marketplace"), t("Signals and copy-trading leaders other users have published. Subscribe to run a signal on your own trade accounts, or follow a leader whose positions are mirrored onto your accounts live — with your own sizing, your own Trading switch and your own logs and alerts. Publishers never see your accounts."), [
        h("button", { class: "btn", onClick: load }, icon("refresh"), t("Refresh")),
        h("button", { class: "btn btn-ghost", onClick: () => navigate("/subscriptions") }, t("Subscription journal"), icon("chevron")),
      ]),
      bar, status, grid,
    );
    if (!(store.get("tradeAccounts") || []).length) actions.loadTradeAccounts();
    load();
    return () => {};
  },
};
