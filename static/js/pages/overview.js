/* Overview: KPIs, connection health, positions, active trades, recent orders — all live. */
import { h, tag, card, fmtTime, fmtDateTime, pageHead, debounce, clear, toast } from "../ui.js";
import { maskAccount } from "../privacy.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { fmtMoney, fmtSigned } from "../charts.js";
import { store, can } from "../store.js";
import { actions } from "../actions.js";
import { dataTable } from "../components/table.js";
import { t } from "../i18n.js";

function kpi(label, iconName) {
  const v = h("div", { class: "v" }, h("span", null, "—"));
  const s = h("div", { class: "s" }, "");
  const el = h("div", { class: "kpi" }, h("div", { class: "k" }, icon(iconName), label), v, s);
  return { el, set(text, tone = "", sub = "") { v.className = "v " + tone; v.firstChild.textContent = text; s.textContent = sub; } };
}

export default {
  title: t("Overview"),
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
    k.trading.el.title = t("Open General & Trading");
    k.trading.el.addEventListener("click", () => navigate("/settings/general"));
    if (k.discord) { k.discord.el.classList.add("clickable"); k.discord.el.addEventListener("click", () => navigate("/discord")); }

    const sessions = dataTable({
      empty: t("No logins configured — add one under Settings → Broker Accounts."),
      columns: [
        { label: t("Login"), render: (x) => h("span", { class: "health-row" }, h("span", { class: `dot ${x.connected ? "on" : ""}` }), x.name || "—") },
        { label: t("Env"), render: (x) => tag((x.environment || "—").toUpperCase(), x.environment === "live" ? "live" : "demo") },
        { label: t("Status"), render: (x) => h("span", { class: x.connected ? "pos" : "neg" }, x.connected ? t("Connected") : t("Disconnected")) },
        { label: t("Token expires"), render: (x) => fmtDateTime(x.token_expires) },
        { label: t("Last renew"), render: (x) => fmtDateTime(x.last_renew) },
        { label: t("Last check"), render: (x) => fmtDateTime(x.last_check) },
        { label: t("Last error"), render: (x) => h("span", { class: x.last_error ? "neg" : "muted" }, x.last_error || "—") },
      ],
    });
    const positions = dataTable({
      empty: t("No open positions"),
      columns: [
        { label: t("Symbol"), render: (p) => p.symbol ?? "—" },
        { label: t("Account"), render: (p) => maskAccount(p.account) || "—" },
        { label: t("Net pos"), className: "num", render: (p) => h("span", { class: (p.netPos ?? 0) >= 0 ? "pos" : "neg" }, String(p.netPos ?? 0)) },
        { label: t("Avg price"), className: "num", render: (p) => p.netPrice ?? "—" },
      ],
    });
    const active = dataTable({
      empty: t("No trades tracked by the bridge right now."),
      columns: [
        { label: t("Webhook"), render: (x) => x.webhook_name || "—" },
        { label: t("Symbol"), render: (x) => x.sym },
        { label: t("Contract"), render: (x) => x.contract || "—" },
        { label: t("Side"), render: (x) => tag((x.side || "").toUpperCase(), x.side) },
        { label: t("Account"), render: (x) => maskAccount(x.account) },
        { label: t("Qty"), className: "num", render: (x) => String(x.qty ?? "—") },
        { label: t("SL order"), render: (x) => String(x.sl_order_id || "—") },
        { label: t("TP orders"), render: (x) => (x.tp_order_ids || []).join(", ") || "—" },
        { label: t("Trade id"), render: (x) => x.trade_id ? h("code", null, x.trade_id) : "—" },
      ],
    });
    const orders = dataTable({
      empty: t("No orders yet"),
      columns: [
        { label: t("Time"), render: (o) => fmtTime(o.ts) },
        { label: t("Action"), render: (o) => tag(o.action || "—", (o.action || "").toLowerCase() === "buy" ? "buy" : (o.action || "").toLowerCase() === "sell" ? "sell" : "") },
        { label: t("Symbol"), render: (o) => [o.symbol || "—", o.simulated ? [" ", tag("SIM", "sim")] : null] },
        { label: t("Account"), render: (o) => maskAccount(o.account) || "—" },
        { label: t("Qty"), className: "num", render: (o) => String(o.qty ?? "—") },
        { label: t("Type"), render: (o) => o.order_type || "—" },
        { label: t("Price"), className: "num", render: (o) => String(o.price ?? o.stop_price ?? "—") },
        { label: t("Status"), render: (o) => tag(o.status || "—", (o.status || "").includes("reject") ? "rejected" : "ok") },
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
        h("strong", null, expired ? t("Contract rollover overdue — ") : t("Contract rollover due — ")),
        t("update the symbol map: "),
        h("ul", { style: "margin:6px 0 8px 18px" }, items.map((w) => h("li", null,
          h("code", null, w.tv_symbol), " → ", h("code", null, w.contract), ` (${w.date_kind} ${w.date}, `,
          w.days_left < 0 ? `${-w.days_left}d ago` : w.days_left === 0 ? "today" : `in ${w.days_left}d`,
          w.source === "broker" ? t(", broker date") : t(", estimated"), t(") → suggested "), h("code", null, w.next)))),
        h("button", { class: "btn btn-sm", onClick: () => navigate("/settings/symbols") }, t("Review & confirm the rollover")));
    }

    // ---- live P&L (today's realised + open, per account) -----------------
    const pnlHero = h("div", { class: "journal-hero" }, "—");
    const pnlSub = h("div", { class: "muted", style: "font-size:12.5px" }, t("Waiting for the first snapshot from the broker…"));
    const pnlRows = h("div", { class: "pnl-rows" });
    const pnlStamp = h("span", { class: "muted", style: "font-size:11px" }, "");
    const pnlTone = (v) => (Number(v) > 0 ? "pos" : Number(v) < 0 ? "neg" : "");
    const money = (v) => h("span", { class: `pnl ${pnlTone(v)}` }, fmtSigned(v, 2));

    // Sort + idle filter, remembered per browser. "activity" = accounts that did
    // something today first (largest realised / open movement on top), idle last.
    const PNL_COLS = [["spec", t("Account")], ["realized", t("Realised")], ["open", t("Open")], ["week", t("Week")], ["cash", t("Balance")], ["dd_room", t("Drawdown")]];
    // Room left to the trailing-drawdown liquidation level: tone by how much of the
    // drawdown is used up. Intraday ("RealTime") trailing is the dangerous one —
    // the level follows the open equity, so a red cell can be minutes from a liquidation.
    const ddTone = (a) => {
      if (a.dd_room == null) return "";
      if (a.dd_room <= 0) return "neg";
      const size = Number(a.dd_size) || 0;
      if (size && a.dd_room < size * 0.25) return "neg";
      if (size && a.dd_room < size * 0.5) return "warn";
      return "pos";
    };
    async function pinThreshold(a) {
      const cur = a.dd_level != null ? String(Math.round(a.dd_level)) : "";
      const v = window.prompt(`Trailing threshold for ${a.spec} as shown by your prop firm / broker (e.g. 48100).\nThe bridge trails it forward from there. Leave empty to reset the tracker.`, cur);
      if (v === null) return;
      try {
        const r = await api.post("/api/pnl/drawdown", { account_id: a.account_id, level: v.trim() === "" ? null : Number(v.replace(/[^0-9.\-]/g, "")) });
        paintPnl(r, true); toast(v.trim() === "" ? t("Drawdown tracker reset") : t("Threshold pinned"), "success");
      } catch (e) { toast(e.message, "error"); }
    }
    const ddCell = (a) => {
      if (a.dd_room == null && a.dd_size == null) return h("span", { class: "muted" }, "—");
      const mode = a.dd_mode ? h("span", { class: `dd-mode${a.dd_mode === "Intraday" ? " intraday" : ""}` }, a.dd_mode) : null;
      const since = a.dd_since ? fmtDateTime(a.dd_since) : "";
      const tip = a.dd_level == null ? t("Max drawdown {size}", { size: fmtMoney(a.dd_size, 0) })
        : t("Peak {peak} − drawdown {size}{cap} = threshold {level}. ", { peak: fmtMoney(a.dd_peak, 2), size: fmtMoney(a.dd_size, 0), cap: a.dd_cap ? t(" (trails up to {cap})", { cap: fmtMoney(a.dd_cap, 0) }) : "", level: fmtMoney(a.dd_level, 2) })
          + (a.dd_seeded ? t("Pinned from your prop firm's figure{since}.", { since: since ? t(" on {when}", { when: since }) : "" }) : t("Tracked by the bridge{since} — pin the exact threshold from your prop firm with ✎ if it differs.", { since: since ? t(" since {when}", { when: since }) : "" }));
      const pin = h("button", { type: "button", class: "dd-pin", title: t("Pin the threshold shown by your prop firm"), onClick: () => pinThreshold(a) }, "✎");
      return h("span", { title: tip },
        a.dd_room == null ? h("span", { class: "muted" }, fmtMoney(a.dd_size, 0)) : h("span", { class: `pnl ${ddTone(a)}` }, fmtSigned(a.dd_room, 2)),
        h("span", { class: "sub" }, a.dd_level != null ? ["level ", fmtMoney(a.dd_level, 0), " "] : null, mode, " ", pin,
          a.dd_level != null && !a.dd_seeded ? h("span", { class: "dd-unpinned", title: t("Peak tracked by the bridge only since it started watching — pin the prop firm's threshold for exact figures") }, "≈") : null));
    };
    let pnlSort = { key: "activity", dir: "desc" };
    let hideIdle = false;
    try {
      pnlSort = JSON.parse(localStorage.getItem("np_pnl_sort") || "null") || pnlSort;
      hideIdle = localStorage.getItem("np_pnl_hide_idle") === "1";
    } catch (e) { /* ignore */ }
    const isIdle = (a) => !Number(a.realized) && !Number(a.open) && !Number(a.week);
    const activity = (a) => Math.abs(Number(a.realized) || 0) + Math.abs(Number(a.open) || 0);
    function sortAccounts(list) {
      const { key, dir } = pnlSort;
      const sgn = dir === "asc" ? 1 : -1;
      return [...list].sort((a, b) => {
        if (key === "activity") {
          const ia = isIdle(a), ib = isIdle(b);
          if (ia !== ib) return ia ? 1 : -1;                       // idle always last
          return (activity(b) - activity(a)) * (dir === "asc" ? -1 : 1) || String(a.spec).localeCompare(String(b.spec));
        }
        if (key === "spec") return String(a.spec || a.account_id).localeCompare(String(b.spec || b.account_id)) * sgn;
        const va = a[key] == null ? (dir === "asc" ? Infinity : -Infinity) : Number(a[key]) || 0;   // no drawdown → last
        const vb = b[key] == null ? (dir === "asc" ? Infinity : -Infinity) : Number(b[key]) || 0;
        return (va - vb) * sgn || String(a.spec).localeCompare(String(b.spec));
      });
    }
    let lastPnl = null;
    let prevValues = new Map();   // account_id → {realized, open, week, cash} for change flashes
    const hideIdleBox = h("input", { type: "checkbox", checked: hideIdle, onChange: (e) => {
      hideIdle = e.target.checked;
      try { localStorage.setItem("np_pnl_hide_idle", hideIdle ? "1" : "0"); } catch (err) { /* ignore */ }
      if (lastPnl) paintPnl(lastPnl, true);
    } });
    const pnlHead = h("div", { class: "pnl-row pnl-head" });
    function paintHead() {
      clear(pnlHead);
      for (const [key, label] of PNL_COLS) {
        const active = pnlSort.key === key;
        pnlHead.append(h("button", { type: "button", class: `pnl-sort${active ? " active" : ""}`, title: `Sort by ${label.toLowerCase()}`, onClick: () => {
          pnlSort = active ? { key, dir: pnlSort.dir === "desc" ? "asc" : "desc" } : { key, dir: key === "spec" ? "asc" : "desc" };
          try { localStorage.setItem("np_pnl_sort", JSON.stringify(pnlSort)); } catch (err) { /* ignore */ }
          if (lastPnl) paintPnl(lastPnl, true);
        } }, label, h("span", { class: "arrow" }, active ? (pnlSort.dir === "asc" ? "▲" : "▼") : "")));
      }
    }
    const riskTag = (a) => a.risk && a.risk.locked ? tag("locked", "warn") : null;
    function cell(k, node, changed) {
      return h("span", { class: `pnl-cell${changed ? " flash" : ""}` }, h("span", { class: "k" }, k), node);
    }
    function paintPnl(p, keepPrev = false) {
      if (!p || !p.ts) return;
      if (!keepPrev && lastPnl && lastPnl.ts === p.ts) return;   // the same snapshot re-emitted by a status poll: nothing to repaint
      lastPnl = p;
      pnlHero.textContent = fmtSigned(p.total, 2);
      pnlHero.className = "journal-hero " + pnlTone(p.total);
      const accounts = p.accounts || [];
      const idle = accounts.filter(isIdle).length;
      clear(pnlSub);
      pnlSub.append(t("Today · realised "), money(p.realized), t(" · open "), money(p.open), t(" · week "), money(p.week),
        ` · ${accounts.length - idle} active`, idle ? ` · ${idle} idle` : "");
      if (p.error) pnlSub.append(h("span", { class: "neg" }, ` · ${p.error}`));
      paintHead();
      clear(pnlRows);
      if (accounts.length) pnlRows.append(pnlHead);
      const next = new Map();
      for (const a of sortAccounts(accounts)) {
        const id = a.account_id;
        const was = keepPrev ? null : prevValues.get(id);
        next.set(id, { realized: a.realized, open: a.open, week: a.week, cash: a.cash, dd_room: a.dd_room });
        const ch = (k) => !!was && Number(was[k]) !== Number(a[k]);
        if (hideIdle && isIdle(a)) continue;
        pnlRows.append(h("div", { class: `pnl-row${isIdle(a) ? " idle" : ""}` },
          h("span", { class: "pnl-acct", title: a.risk && a.risk.locked ? `Risk guard: ${a.risk.reason}` : null }, maskAccount(a.spec || String(id)), a.environment === "live" ? [" ", tag("live", "accent")] : null, riskTag(a) ? [" ", riskTag(a)] : null),
          cell("realised", money(a.realized), ch("realized")),
          cell("open", money(a.open), ch("open")),
          cell("week", money(a.week), ch("week")),
          cell("balance", h("span", null, fmtMoney(a.cash, 2)), ch("cash")),
          cell("max drawdown", ddCell(a), ch("dd_room"))));
      }
      if (!keepPrev) prevValues = next;
      if (!accounts.length) pnlRows.append(h("div", { class: "muted" }, t("No connected trade account — connect a login under Settings → Broker Accounts.")));
      else if (hideIdle && idle === accounts.length) pnlRows.append(h("div", { class: "muted" }, t("All accounts are idle today — untick “Hide idle” to see them.")));
      pnlStamp.textContent = t("updated {when}", { when: fmtTime(p.ts) });
    }
    const pnlCard = card({ title: t("Today's P&L"), actions: [pnlStamp,
      h("label", { class: "pnl-toggle", title: t("Hide accounts with no realised, open or weekly P&L today") }, hideIdleBox, t(" Hide idle")),
      h("button", { class: "btn btn-ghost btn-sm", title: t("Refresh now"), onClick: async () => { try { paintPnl(await api.get("/api/pnl?refresh=1")); } catch (e) { toast(e.message, "error"); } } }, icon("refresh"))] },
      pnlHero, pnlSub, pnlRows);
    pnlCard.classList.add("pnl-card");

    root.append(
      pageHead(t("Overview"), t("Live view of your bridge: broker sessions, positions, tracked trades and the latest orders."), [
        h("button", { class: "btn", onClick: () => actions.healthCheck() }, icon("refresh"), t("Check connections")),
      ]),
      rollover,
      pnlCard,
      h("div", { class: "kpis" }, Object.values(k).filter(Boolean).map((x) => x.el)),
      card({ title: t("Connection health"), actions: [h("button", { class: "btn btn-ghost btn-sm", onClick: () => navigate("/settings/accounts") }, t("Manage logins"))] }, sessions.el),
      h("div", { class: "grid grid-2" },
        card({ title: t("Open positions"), actions: [h("button", { class: "btn btn-ghost btn-sm", onClick: () => actions.refreshPositions() }, icon("refresh"), t("Refresh"))] }, positions.el),
        card({ title: t("Active trades"), hint: t("Positions the bridge is managing (stop / targets / partial closes).") }, active.el)),
      card({ title: t("Recent orders") }, orders.el),
    );

    const refreshPositionsSoon = debounce(() => actions.refreshPositions(), 1500);
    const unsubs = [
      store.subscribe("pnl", (p) => paintPnl(p), { immediate: true }),   // subscribers get (value, key): never pass the key as keepPrev
      store.subscribe("status", (s) => {
        if (!s) return;
        const c = s.connection || {};
        const total = c.accounts_total || 0, con = c.accounts_connected || 0;
        k.trading.set(s.trading_enabled ? t("ENABLED") : t("DISABLED"), s.trading_enabled ? "on" : "off", s.trading_enabled ? t("signals execute") : t("signals are logged only"));
        k.logins.set(total ? `${con}/${total}` : "—", total ? (con ? "on" : "off") : "", total ? t("connected") : t("none configured"));
        const ta = s.trade_accounts || [];
        const taConn = ta.filter((a) => a.connected).length;
        k.accounts.set(ta.length ? `${taConn}/${ta.length}` : "—", ta.length ? (taConn ? "on" : "off") : "", ta.length ? t("connected") : t("discover under Settings"));
        const rows = [];
        for (const [key, t] of Object.entries(s.active_trades || {})) {
          const accts = t.accounts || {};
          for (const [id, a] of Object.entries(accts)) {
            rows.push({ ...t, sym: t.root || key.split(":").pop(), account: a.name || id, qty: a.qty, sl_order_id: a.sl_order_id, tp_order_ids: a.tp_order_ids });
          }
        }
        k.trades.set(String(rows.length), rows.length ? "on" : "", rows.length ? t("managed by the bridge") : "flat");
        active.update(rows);
        sessions.update(s.sessions || []);
        paintRollover(s.rollover);
      }, { immediate: true }),
      store.subscribe("orders", (o) => { orders.update((o || []).slice(0, 50)); refreshPositionsSoon(); }, { immediate: true }),
      store.subscribe("positions", (p) => {
        if (p && p.error) { positions.update([]); positions.tbody.firstChild.firstChild.textContent = p.error; return; }
        positions.update(p || []);
      }, { immediate: true }),
      store.subscribe("stream", (s) => k.stream.set(s === "live" ? t("Live") : s === "reconnecting" ? t("Reconnecting…") : t("Offline"), s === "live" ? "on" : s === "reconnecting" ? "warn" : "off", t("event stream")), { immediate: true }),
      k.discord ? store.subscribe("discordStatus", (d) => {
        if (!d) return;
        const label = d.state === "token_invalid" ? t("Token rejected") : d.health === "down" ? t("Offline")
          : ({ connected: "Connected", connecting: "Connecting…", disabled: "Disabled", error: "Error", library_missing: "No library", stopped: "Stopped", not_entitled: "Not enabled" }[d.state] || d.state);
        k.discord.set(d.enabled ? label : t("Off"), d.state === "connected" ? "on" : (d.health === "down" || d.state === "error") ? "off" : "", d.user ? `as ${d.user}` : `${(d.watched_channels || []).length} channel(s) watched`);
      }, { immediate: true }) : null,
    ].filter(Boolean);

    if (store.get("positions") == null) actions.refreshPositions();
    return () => { unsubs.forEach((u) => u()); refreshPositionsSoon.cancel && refreshPositionsSoon.cancel(); };
  },
};
