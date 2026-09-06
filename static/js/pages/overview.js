/* Overview: KPIs, connection health, positions, active trades, recent orders — all live. */
import { h, tag, card, fmtTime, fmtDateTime, pageHead, debounce, clear, toast } from "../ui.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { fmtMoney, fmtSigned } from "../charts.js";
import { store, can } from "../store.js";
import { actions } from "../actions.js";
import { dataTable } from "../components/table.js";

function kpi(label, iconName) {
  const v = h("div", { class: "v" }, h("span", null, "—"));
  const s = h("div", { class: "s" }, "");
  const el = h("div", { class: "kpi" }, h("div", { class: "k" }, icon(iconName), label), v, s);
  return { el, set(text, tone = "", sub = "") { v.className = "v " + tone; v.firstChild.textContent = text; s.textContent = sub; } };
}

export default {
  title: "Overview",
  render(root, { navigate }) {
    const me = store.get("me");
    const k = {
      trading: kpi("Trading", "zap"),
      logins: kpi("Logins", "key"),
      accounts: kpi("Trade accounts", "users"),
      trades: kpi("Active trades", "activity"),
      discord: can(me, "discord") ? kpi("Discord listener", "discord") : null,
      stream: kpi("Live updates", "wifi"),
    };
    k.trading.el.classList.add("clickable");
    k.trading.el.title = "Open General & Trading";
    k.trading.el.addEventListener("click", () => navigate("/settings/general"));
    if (k.discord) { k.discord.el.classList.add("clickable"); k.discord.el.addEventListener("click", () => navigate("/discord")); }

    const sessions = dataTable({
      empty: "No logins configured — add one under Settings → Tradovate Accounts.",
      columns: [
        { label: "Login", render: (x) => h("span", { class: "health-row" }, h("span", { class: `dot ${x.connected ? "on" : ""}` }), x.name || "—") },
        { label: "Env", render: (x) => tag((x.environment || "—").toUpperCase(), x.environment === "live" ? "live" : "demo") },
        { label: "Status", render: (x) => h("span", { class: x.connected ? "pos" : "neg" }, x.connected ? "Connected" : "Disconnected") },
        { label: "Token expires", render: (x) => fmtDateTime(x.token_expires) },
        { label: "Last renew", render: (x) => fmtDateTime(x.last_renew) },
        { label: "Last check", render: (x) => fmtDateTime(x.last_check) },
        { label: "Last error", render: (x) => h("span", { class: x.last_error ? "neg" : "muted" }, x.last_error || "—") },
      ],
    });
    const positions = dataTable({
      empty: "No open positions",
      columns: [
        { label: "Symbol", render: (p) => p.symbol ?? "—" },
        { label: "Account", render: (p) => p.account ?? "—" },
        { label: "Net pos", className: "num", render: (p) => h("span", { class: (p.netPos ?? 0) >= 0 ? "pos" : "neg" }, String(p.netPos ?? 0)) },
        { label: "Avg price", className: "num", render: (p) => p.netPrice ?? "—" },
      ],
    });
    const active = dataTable({
      empty: "No trades tracked by the bridge right now.",
      columns: [
        { label: "Webhook", render: (t) => t.webhook_name || "—" },
        { label: "Symbol", render: (t) => t.sym },
        { label: "Contract", render: (t) => t.contract || "—" },
        { label: "Side", render: (t) => tag((t.side || "").toUpperCase(), t.side) },
        { label: "Account", render: (t) => t.account },
        { label: "Qty", className: "num", render: (t) => String(t.qty ?? "—") },
        { label: "SL order", render: (t) => String(t.sl_order_id || "—") },
        { label: "TP orders", render: (t) => (t.tp_order_ids || []).join(", ") || "—" },
        { label: "Trade id", render: (t) => t.trade_id ? h("code", null, t.trade_id) : "—" },
      ],
    });
    const orders = dataTable({
      empty: "No orders yet",
      columns: [
        { label: "Time", render: (o) => fmtTime(o.ts) },
        { label: "Action", render: (o) => tag(o.action || "—", (o.action || "").toLowerCase() === "buy" ? "buy" : (o.action || "").toLowerCase() === "sell" ? "sell" : "") },
        { label: "Symbol", render: (o) => [o.symbol || "—", o.simulated ? [" ", tag("SIM", "sim")] : null] },
        { label: "Account", render: (o) => o.account || "—" },
        { label: "Qty", className: "num", render: (o) => String(o.qty ?? "—") },
        { label: "Type", render: (o) => o.order_type || "—" },
        { label: "Price", className: "num", render: (o) => String(o.price ?? o.stop_price ?? "—") },
        { label: "Status", render: (o) => tag(o.status || "—", (o.status || "").includes("reject") ? "rejected" : "ok") },
      ],
    });

    const rollover = h("div", { class: "callout warn", hidden: true });
    function paintRollover(list) {
      clear(rollover);
      const items = list || [];
      rollover.hidden = !items.length;
      if (!items.length) return;
      const expired = items.some((w) => w.stage === "expired");
      rollover.classList.toggle("danger", expired);
      rollover.classList.toggle("warn", !expired);
      rollover.append(
        h("strong", null, expired ? "Contract rollover overdue — " : "Contract rollover due — "),
        "update the symbol map: ",
        h("ul", { style: "margin:6px 0 8px 18px" }, items.map((w) => h("li", null,
          h("code", null, w.tv_symbol), " → ", h("code", null, w.contract), ` (${w.date_kind} ${w.date}, `,
          w.days_left < 0 ? `${-w.days_left}d ago` : w.days_left === 0 ? "today" : `in ${w.days_left}d`,
          w.source === "broker" ? ", broker date" : ", estimated", ") → suggested ", h("code", null, w.next)))),
        h("button", { class: "btn btn-sm", onClick: () => navigate("/settings/symbols") }, "Open symbol map"));
    }

    // ---- live P&L (today's realised + open, per account) -----------------
    const pnlHero = h("div", { class: "journal-hero" }, "—");
    const pnlSub = h("div", { class: "muted", style: "font-size:12.5px" }, "Waiting for the first snapshot from Tradovate…");
    const pnlRows = h("div", { class: "pnl-rows" });
    const pnlStamp = h("span", { class: "muted", style: "font-size:11px" }, "");
    const pnlTone = (v) => (Number(v) > 0 ? "pos" : Number(v) < 0 ? "neg" : "");
    const money = (v) => h("span", { class: `pnl ${pnlTone(v)}` }, fmtSigned(v, 2));
    function paintPnl(p) {
      if (!p || !p.ts) return;
      pnlHero.textContent = fmtSigned(p.total, 2);
      pnlHero.className = "journal-hero " + pnlTone(p.total);
      clear(pnlSub);
      pnlSub.append("Today · realised ", money(p.realized), " · open ", money(p.open), " · week ", money(p.week));
      if (p.error) pnlSub.append(h("span", { class: "neg" }, ` · ${p.error}`));
      clear(pnlRows);
      for (const a of p.accounts || []) {
        pnlRows.append(h("div", { class: "pnl-row" },
          h("span", { class: "pnl-acct" }, a.spec || String(a.account_id), a.environment === "live" ? [" ", tag("live", "accent")] : null),
          h("span", { class: "pnl-cell" }, h("span", { class: "k" }, "realised"), money(a.realized)),
          h("span", { class: "pnl-cell" }, h("span", { class: "k" }, "open"), money(a.open)),
          h("span", { class: "pnl-cell" }, h("span", { class: "k" }, "week"), money(a.week)),
          h("span", { class: "pnl-cell" }, h("span", { class: "k" }, "balance"), h("span", null, fmtMoney(a.cash, 2)))));
      }
      if (!(p.accounts || []).length) pnlRows.append(h("div", { class: "muted" }, "No connected trade account — connect a login under Settings → Tradovate Accounts."));
      pnlStamp.textContent = `updated ${fmtTime(p.ts)}`;
    }
    const pnlCard = card({ title: "Today's P&L", actions: [pnlStamp,
      h("button", { class: "btn btn-ghost btn-sm", title: "Refresh now", onClick: async () => { try { paintPnl(await api.get("/api/pnl?refresh=1")); } catch (e) { toast(e.message, "error"); } } }, icon("refresh"))] },
      pnlHero, pnlSub, pnlRows);
    pnlCard.classList.add("pnl-card");

    root.append(
      pageHead("Overview", "Live view of your bridge: broker sessions, positions, tracked trades and the latest orders.", [
        h("button", { class: "btn", onClick: () => actions.healthCheck() }, icon("refresh"), "Check connections"),
      ]),
      rollover,
      pnlCard,
      h("div", { class: "kpis" }, Object.values(k).filter(Boolean).map((x) => x.el)),
      card({ title: "Connection health", actions: [h("button", { class: "btn btn-ghost btn-sm", onClick: () => navigate("/settings/accounts") }, "Manage logins")] }, sessions.el),
      h("div", { class: "grid grid-2" },
        card({ title: "Open positions", actions: [h("button", { class: "btn btn-ghost btn-sm", onClick: () => actions.refreshPositions() }, icon("refresh"), "Refresh")] }, positions.el),
        card({ title: "Active trades", hint: "Positions the bridge is managing (stop / targets / partial closes)." }, active.el)),
      card({ title: "Recent orders" }, orders.el),
    );

    const refreshPositionsSoon = debounce(() => actions.refreshPositions(), 1500);
    const unsubs = [
      store.subscribe("pnl", paintPnl, { immediate: true }),
      store.subscribe("status", (s) => {
        if (!s) return;
        const c = s.connection || {};
        const total = c.accounts_total || 0, con = c.accounts_connected || 0;
        k.trading.set(s.trading_enabled ? "ENABLED" : "DISABLED", s.trading_enabled ? "on" : "off", s.trading_enabled ? "signals execute" : "signals are logged only");
        k.logins.set(total ? `${con}/${total}` : "—", total ? (con ? "on" : "off") : "", total ? "connected" : "none configured");
        const ta = s.trade_accounts || [];
        const taConn = ta.filter((a) => a.connected).length;
        k.accounts.set(ta.length ? `${taConn}/${ta.length}` : "—", ta.length ? (taConn ? "on" : "off") : "", ta.length ? "connected" : "discover under Settings");
        const rows = [];
        for (const [key, t] of Object.entries(s.active_trades || {})) {
          const accts = t.accounts || {};
          for (const [id, a] of Object.entries(accts)) {
            rows.push({ ...t, sym: t.root || key.split(":").pop(), account: a.name || id, qty: a.qty, sl_order_id: a.sl_order_id, tp_order_ids: a.tp_order_ids });
          }
        }
        k.trades.set(String(rows.length), rows.length ? "on" : "", rows.length ? "managed by the bridge" : "flat");
        active.update(rows);
        sessions.update(s.sessions || []);
        paintRollover(s.rollover);
      }, { immediate: true }),
      store.subscribe("orders", (o) => { orders.update((o || []).slice(0, 50)); refreshPositionsSoon(); }, { immediate: true }),
      store.subscribe("positions", (p) => {
        if (p && p.error) { positions.update([]); positions.tbody.firstChild.firstChild.textContent = p.error; return; }
        positions.update(p || []);
      }, { immediate: true }),
      store.subscribe("stream", (s) => k.stream.set(s === "live" ? "Live" : s === "reconnecting" ? "Reconnecting…" : "Offline", s === "live" ? "on" : s === "reconnecting" ? "warn" : "off", "event stream"), { immediate: true }),
      k.discord ? store.subscribe("discordStatus", (d) => {
        if (!d) return;
        const label = d.state === "token_invalid" ? "Token rejected" : d.health === "down" ? "Offline"
          : ({ connected: "Connected", connecting: "Connecting…", disabled: "Disabled", error: "Error", library_missing: "No library", stopped: "Stopped", not_entitled: "Not enabled" }[d.state] || d.state);
        k.discord.set(d.enabled ? label : "Off", d.state === "connected" ? "on" : (d.health === "down" || d.state === "error") ? "off" : "", d.user ? `as ${d.user}` : `${(d.watched_channels || []).length} channel(s) watched`);
      }, { immediate: true }) : null,
    ].filter(Boolean);

    if (store.get("positions") == null) actions.refreshPositions();
    return () => unsubs.forEach((u) => u());
  },
};
