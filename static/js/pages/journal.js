/* Trading journal: imported Tradovate trades with P&L reporting per day / week /
   month, equity curve, calendar, breakdowns, notes & tags, import on demand. */
import { h, card, tag, fmtDateTime, fmtNum, pageHead, clear, toast } from "../ui.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { dataTable } from "../components/table.js";
import { openDrawer, closeDrawer } from "../components/drawer.js";
import { columnChart, lineChart, barList, calendarHeatmap, mount, fmtMoney, fmtSigned } from "../charts.js";

const RANGES = [["today", "Today"], ["week", "This week"], ["month", "This month"], ["30d", "Last 30 days"], ["90d", "Last 90 days"], ["ytd", "Year to date"], ["all", "All time"]];
const PERIODS = [["day", "Daily"], ["week", "Weekly"], ["month", "Monthly"]];
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function kpi(label) {
  const v = h("div", { class: "v" }, h("span", null, "—"));
  const s = h("div", { class: "s muted" }, "");
  const el = h("div", { class: "kpi" }, h("div", { class: "k" }, label), v, s);
  return { el, set(text, tone = "", sub = "") { v.className = "v " + tone; v.firstChild.textContent = text; s.textContent = sub; } };
}

const pnl = (v, digits = 2) => h("span", { class: `pnl ${Number(v) > 0 ? "pos" : Number(v) < 0 ? "neg" : ""}` }, fmtSigned(v, digits));
const bucketLabel = (b, period) => period === "month" ? b.bucket : period === "week" ? b.bucket.replace(/^\d{4}-/, "") : b.bucket.slice(5);

/** A chart card with a "Table" twin toggle — every chart stays readable without colour. */
function vizCard(title, hint, chartEl, tableEl) {
  const toggle = h("button", { type: "button", class: "btn btn-ghost btn-sm viz-table-toggle" }, "Table");
  const body = h("div", null, chartEl);
  let showing = false;
  toggle.addEventListener("click", () => { showing = !showing; toggle.textContent = showing ? "Chart" : "Table"; clear(body); body.append(showing ? tableEl : chartEl); });
  return card({ title, hint, actions: [toggle] }, body);
}

export default {
  title: "Journal",
  render(root, { navigate }) {
    const f = { range: "month", period: "day", account: "", symbol: "", side: "" };
    let month = new Date().toISOString().slice(0, 7);

    // ---- filter row (one row, scopes everything below) ----------------
    const rangeSel = h("select", { class: "input-sm", onChange: (e) => { f.range = e.target.value; load(); } }, RANGES.map(([v, l]) => h("option", { value: v, selected: v === f.range }, l)));
    const periodSel = h("select", { class: "input-sm", onChange: (e) => { f.period = e.target.value; load(); } }, PERIODS.map(([v, l]) => h("option", { value: v, selected: v === f.period }, l)));
    const accountSel = h("select", { class: "input-sm", onChange: (e) => { f.account = e.target.value; load(); } }, h("option", { value: "" }, "All accounts"));
    const symbolSel = h("select", { class: "input-sm", onChange: (e) => { f.symbol = e.target.value; load(); } }, h("option", { value: "" }, "All symbols"));
    const sideSel = h("select", { class: "input-sm", onChange: (e) => { f.side = e.target.value; load(); } },
      h("option", { value: "" }, "Long + short"), h("option", { value: "long" }, "Long only"), h("option", { value: "short" }, "Short only"));
    const importBtn = h("button", { type: "button", class: "btn btn-primary", onClick: importNow }, icon("download"), "Import now");
    const csvBtn = h("button", { type: "button", class: "btn", onClick: () => openCsvImport() }, icon("inbox"), "Import CSV");
    const importInfo = h("span", { class: "muted", style: "font-size:12px" }, "");
    const exportLink = h("a", { class: "btn btn-ghost btn-sm", href: "#", onClick: (e) => { e.preventDefault(); window.open(`/api/journal/export.csv?${qs()}`, "_blank"); } }, icon("external"), "CSV");

    // ---- KPIs ----------------------------------------------------------
    const hero = h("div", { class: "journal-hero" }, "—");
    const heroSub = h("div", { class: "muted", style: "font-size:12.5px" }, "");
    const k = { win: kpi("Win rate"), pf: kpi("Profit factor"), trades: kpi("Trades"), avg: kpi("Avg win / loss"), exp: kpi("Expectancy / trade"), dd: kpi("Max drawdown"), fees: kpi("Fees"), days: kpi("Trading days") };

    // ---- chart holders -------------------------------------------------
    const periodChart = h("div"), periodTable = h("div");
    const equityChart = h("div"), equityTable = h("div");
    const calBox = h("div"), calTable = h("div");
    const calTitle = h("span", null, month);
    const bySymbol = h("div"), bySymbolTable = h("div");
    const byWeekday = h("div"), byWeekdayTable = h("div");
    const byHour = h("div"), byHourTable = h("div");
    const byAccount = h("div"), byAccountTable = h("div");

    // ---- trades table --------------------------------------------------
    const trades = dataTable({ empty: "No trades in this range — import from Tradovate or widen the range.", compact: true,
      onRow: (t) => openTrade(t),
      columns: [
        { label: "Closed", render: (t) => fmtDateTime(t.exit_ts) },
        { label: "Account", render: (t) => t.account_name || t.account_spec },
        { label: "Symbol", render: (t) => h("code", null, t.symbol) },
        { label: "Side", render: (t) => tag(t.side, t.side === "long" ? "buy" : "sell") },
        { label: "Qty", className: "num", render: (t) => String(t.qty) },
        { label: "Entry → Exit", className: "num", render: (t) => `${fmtNum(t.entry_price, 4)} → ${fmtNum(t.exit_price, 4)}` },
        { label: "Points", className: "num", render: (t) => fmtNum(t.points, 4) },
        { label: "Gross", className: "num", render: (t) => pnl(t.gross_pnl) },
        { label: "Fees", className: "num", render: (t) => fmtMoney(t.fees, 2) },
        { label: "Net", className: "num", render: (t) => pnl(t.net_pnl) },
        { label: "Notes", render: (t) => h("span", { class: "journal-note" }, (t.tags || []).map((x) => tag(x)), t.note ? ` ${t.note}` : t.tags?.length ? "" : "—") },
      ] });
    const moreBtn = h("button", { type: "button", class: "btn btn-sm", onClick: () => loadTrades(true) }, "Load more");
    let tradeRows = [], nextBefore = null;

    const imports = dataTable({ empty: "No imports yet", compact: true, columns: [
      { label: "When", render: (r) => fmtDateTime(r.ts) },
      { label: "Trigger", render: (r) => r.trigger + (r.by ? ` (${r.by})` : "") },
      { label: "Status", render: (r) => tag(r.status, r.status === "ok" ? "ok" : r.status === "partial" ? "warn" : "error") },
      { label: "Logins", className: "num", render: (r) => String(r.logins) },
      { label: "Fills (new)", className: "num", render: (r) => `${r.fills} (${r.fills_new})` },
      { label: "Trades (new)", className: "num", render: (r) => `${r.trades} (${r.trades_new})` },
      { label: "Error", render: (r) => r.error ? h("span", { class: "neg" }, r.error) : "—" },
    ] });

    function qs(extra = {}) {
      return new URLSearchParams({ range: f.range, period: f.period, account: f.account, symbol: f.symbol, side: f.side, ...extra }).toString();
    }

    // ---- rendering ------------------------------------------------------
    function fillOptions(sel, options, current) {
      const first = sel.firstChild;
      clear(sel); sel.append(first);
      for (const [v, l] of options) sel.append(h("option", { value: v, selected: v === current }, l));
    }

    function paintOverview(ov) {
      const st = ov.stats;
      hero.textContent = fmtSigned(st.net_pnl, 2);
      hero.className = "journal-hero " + (st.net_pnl > 0 ? "pos" : st.net_pnl < 0 ? "neg" : "");
      heroSub.textContent = `Net P&L · ${RANGES.find(([v]) => v === f.range)?.[1] || f.range} · gross ${fmtSigned(st.gross_pnl, 2)} · ${ov.range.timezone}`;
      k.win.set(st.trades ? `${Math.round(st.win_rate * 100)}%` : "—", "", st.trades ? `${st.wins} W / ${st.losses} L` : "");
      k.pf.set(st.profit_factor == null ? (st.wins ? "∞" : "—") : String(st.profit_factor), st.profit_factor >= 1.5 ? "on" : st.profit_factor && st.profit_factor < 1 ? "off" : "", "gross win ÷ gross loss");
      k.trades.set(String(st.trades), "", `${st.contracts} contracts`);
      k.avg.set(st.trades ? `${fmtSigned(st.avg_win)} / ${fmtSigned(st.avg_loss)}` : "—", "", `best ${fmtSigned(st.largest_win)} · worst ${fmtSigned(st.largest_loss)}`);
      k.exp.set(st.trades ? fmtSigned(st.expectancy, 2) : "—", st.expectancy > 0 ? "on" : st.expectancy < 0 ? "off" : "", "net ÷ trades");
      k.dd.set(st.trades ? fmtSigned(st.max_drawdown, 2) : "—", st.max_drawdown < 0 ? "warn" : "", "peak-to-trough of the equity curve");
      k.fees.set(fmtMoney(st.fees, 2), "", "commissions + exchange fees");
      k.days.set(String(st.trading_days), "", st.trading_days ? `${fmtSigned(st.avg_per_day)} per day` : "");

      const period = f.period;
      const buckets = ov.summary;
      const cols = buckets.map((b) => ({ label: bucketLabel(b, period), tipLabel: b.bucket, value: b.net_pnl, sub: `${b.trades} trades · ${Math.round(b.win_rate * 100)}% win` }));
      mount(periodChart, (w) => columnChart(cols, { width: w, onSelect: (d) => { if (period === "day") { month = d.tipLabel.slice(0, 7); loadCalendar(); } } }));
      clear(periodTable); periodTable.append(bucketTable(buckets));
      const pts = buckets.map((b) => ({ x: bucketLabel(b, period), tip: b.bucket, value: b.cumulative }));
      mount(equityChart, (w) => lineChart(pts, { width: w }));
      clear(equityTable); equityTable.append(simpleTable(["Period", "Net", "Cumulative"], buckets.map((b) => [b.bucket, pnl(b.net_pnl), pnl(b.cumulative)])));

      const bd = (rows, keyFn) => rows.map((r) => ({ label: keyFn(r.key), value: r.net_pnl, sub: `${r.trades} trades · ${Math.round(r.win_rate * 100)}% win` }));
      const bt = (rows, keyFn) => simpleTable(["Key", "Trades", "Win rate", "Net"], rows.map((r) => [keyFn(r.key), String(r.trades), `${Math.round(r.win_rate * 100)}%`, pnl(r.net_pnl)]));
      clear(bySymbol); bySymbol.append(barList(bd(st.by_symbol, (x) => x)));
      clear(bySymbolTable); bySymbolTable.append(bt(st.by_symbol, (x) => x));
      clear(byAccount); byAccount.append(barList(bd(st.by_account, (x) => x)));
      clear(byAccountTable); byAccountTable.append(bt(st.by_account, (x) => x));
      const wd = [...st.by_weekday].sort((a, b) => a.key - b.key);
      const wdCols = wd.map((r) => ({ label: WEEKDAYS[r.key] || String(r.key), value: r.net_pnl, sub: `${r.trades} trades` }));
      mount(byWeekday, (w) => columnChart(wdCols, { width: w, height: 170 }));
      clear(byWeekdayTable); byWeekdayTable.append(bt(wd, (x) => WEEKDAYS[x] || String(x)));
      const hr = [...st.by_hour].sort((a, b) => a.key - b.key);
      const hrCols = hr.map((r) => ({ label: `${String(r.key).padStart(2, "0")}h`, value: r.net_pnl, sub: `${r.trades} trades` }));
      mount(byHour, (w) => columnChart(hrCols, { width: w, height: 170, maxLabels: 24 }));
      clear(byHourTable); byHourTable.append(bt(hr, (x) => `${String(x).padStart(2, "0")}:00`));

      fillOptions(accountSel, ov.accounts.map((a) => [a.account_spec || String(a.account_id), `${a.account_name || a.account_spec} (${a.n})`]), f.account);
      fillOptions(symbolSel, ov.symbols.map((s) => [s, s]), f.symbol);
      importInfo.textContent = (ov.last_import ? `Last import ${fmtDateTime(ov.last_import)}` : "Never imported") +
        (ov.schedule.enabled ? ` · daily at ${ov.schedule.time} ${ov.schedule.timezone}` : " · auto-import off");
      importInfo.dataset.tz = ov.schedule.timezone;
      knownAccounts = [...new Set([...(ov.accounts || []).map((a) => a.account_spec || a.account_name), ...(ov.trade_accounts || [])].filter(Boolean))];
    }

    function bucketTable(buckets) {
      return simpleTable(["Period", "Trades", "Win rate", "Gross", "Fees", "Net", "Cumulative"],
        buckets.map((b) => [b.bucket, String(b.trades), `${Math.round(b.win_rate * 100)}%`, pnl(b.gross_pnl), fmtMoney(b.fees, 2), pnl(b.net_pnl), pnl(b.cumulative)]));
    }
    function simpleTable(head, rows) {
      return h("div", { class: "table-scroll" }, h("table", { class: "data-table compact" },
        h("thead", null, h("tr", null, head.map((x) => h("th", null, x)))),
        h("tbody", null, rows.length ? rows.map((r) => h("tr", null, r.map((c) => h("td", null, c)))) : h("tr", null, h("td", { class: "empty", colspan: head.length }, "No data")))));
    }

    async function loadCalendar() {
      try {
        const cal = await api.get(`/api/journal/calendar?month=${month}&account=${encodeURIComponent(f.account)}&symbol=${encodeURIComponent(f.symbol)}`);
        calTitle.textContent = `${month} · ${fmtSigned(cal.net_pnl, 2)} · ${cal.trades} trades`;
        clear(calBox); calBox.append(calendarHeatmap(cal.days, { onSelect: (d) => showDay(d.day) }));
        clear(calTable); calTable.append(simpleTable(["Day", "Trades", "Net"], cal.days.filter((d) => d.trades).map((d) => [d.day, String(d.trades), pnl(d.net_pnl)])));
      } catch (e) { toast(e.message, "error"); }
    }
    function shiftMonth(delta) {
      const [y, m] = month.split("-").map(Number);
      const d = new Date(Date.UTC(y, m - 1 + delta, 1));
      month = d.toISOString().slice(0, 7);
      loadCalendar();
    }
    async function showDay(day) {
      const r = await api.get(`/api/journal/trades?frm=${day}&to=${day}&account=${encodeURIComponent(f.account)}&symbol=${encodeURIComponent(f.symbol)}&limit=500`);
      const t = dataTable({ compact: true, onRow: (x) => openTrade(x), columns: [
        { label: "Closed", render: (x) => fmtDateTime(x.exit_ts) }, { label: "Symbol", render: (x) => x.symbol },
        { label: "Side", render: (x) => x.side }, { label: "Qty", render: (x) => String(x.qty) }, { label: "Net", render: (x) => pnl(x.net_pnl) },
      ] });
      t.update(r.items);
      openDrawer({ title: `Trades on ${day}`, body: t.el, width: "560px" });
    }

    async function loadTrades(more = false) {
      try {
        const r = await api.get(`/api/journal/trades?${qs({ limit: "100", ...(more && nextBefore ? { before: String(nextBefore) } : {}) })}`);
        tradeRows = more ? tradeRows.concat(r.items) : r.items;
        nextBefore = r.next_before;
        moreBtn.hidden = !nextBefore;
        trades.update(tradeRows);
      } catch (e) { toast(e.message, "error"); }
    }

    let loading = false;
    async function load() {
      if (loading) return;
      loading = true;
      root.classList.add("refetching");   // hold the previous render, no skeleton flash
      try {
        const [ov] = await Promise.all([api.get(`/api/journal/overview?${qs()}`), loadTrades(), loadCalendar(), loadImports()]);
        paintOverview(ov);
      } catch (e) { toast(e.message, "error"); }
      finally { root.classList.remove("refetching"); loading = false; }
    }
    async function loadImports() { try { imports.update(await api.get("/api/journal/imports")); } catch (e) { /* ignore */ } }

    async function importNow() {
      importBtn.disabled = true; importBtn.textContent = "Importing…";
      try {
        const r = await api.post("/api/journal/import");
        toast(r.status === "ok" ? `Imported ${r.trades_new} new trade(s), ${r.fills_new} new fill(s)` : `Import ${r.status}: ${r.error || ""}`, r.status === "ok" ? "success" : "error");
        await load();
      } catch (e) { toast(e.message, "error"); }
      finally { importBtn.disabled = false; clear(importBtn); importBtn.append(icon("download"), "Import now"); }
    }

    let knownAccounts = [];
    function openCsvImport() {
      const file = h("input", { type: "file", accept: ".csv,text/csv", class: "input" });
      const accountInput = h("input", { class: "input", list: "journal-accounts", placeholder: "e.g. DEMO12345 or Apex 50k", value: knownAccounts[0] || "" });
      const datalist = h("datalist", { id: "journal-accounts" }, knownAccounts.map((a) => h("option", { value: a })));
      const tzInput = h("input", { class: "input", placeholder: "Europe/Zurich", value: (importInfo.dataset.tz || "") });
      const feeInput = h("input", { class: "input", type: "number", step: "0.01", min: "0", value: "0", style: "max-width:140px" });
      const result = h("div", { class: "muted", style: "font-size:12.5px;white-space:pre-wrap" });
      const go = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
        if (!file.files || !file.files[0]) { toast("Choose a CSV file first", "error"); return; }
        go.disabled = true; go.textContent = "Importing…";
        try {
          const fd = new FormData();
          fd.append("file", file.files[0]);
          fd.append("account", accountInput.value.trim());
          fd.append("timezone", tzInput.value.trim());
          fd.append("fee_per_side", feeInput.value || "0");
          const res = await fetch("/api/journal/import-csv", { method: "POST", body: fd, credentials: "same-origin" });
          const data = await res.json().catch(() => ({}));
          if (!res.ok) throw new Error(data.detail || res.statusText);
          result.textContent = `${data.format} export: ${data.rows ?? data.fills} rows → ${data.trades} trades, ${data.trades_new} new, ${data.duplicates} already known, ${data.skipped} rows skipped.`
            + (data.skipped_rows?.length ? "\n" + data.skipped_rows.join("\n") : "");
          toast(`${data.trades_new} new trade(s) imported`, "success");
          await load();
        } catch (e) { toast(e.message, "error"); result.textContent = e.message; }
        finally { go.disabled = false; go.textContent = "Import file"; }
      } }, "Import file");
      openDrawer({ title: "Import a Tradovate CSV export", width: "560px",
        body: h("div", null,
          h("p", { class: "hint" }, "Tradovate's API only exposes the current session, so past days come from the platform's own reports: in Tradovate open ",
            h("strong", null, "Reports → Performance"), " (best: one row per round trip with P&L), select the account and date range, and export the CSV. ",
            h("strong", null, "Orders"), " exports (filled orders) are paired FIFO instead. Trades already imported via the API are recognised and skipped."),
          h("div", { class: "field" }, h("label", null, "CSV file"), file),
          h("div", { class: "field" }, h("label", null, "Account the export belongs to"), accountInput, datalist,
            h("div", { class: "field-hint" }, "Use the Tradovate account name (spec) to merge with API imports; any other label creates a separate manual account.")),
          h("div", { class: "grid grid-2" },
            h("div", { class: "field" }, h("label", null, "Timestamps are in timezone"), tzInput, h("div", { class: "field-hint" }, "The timezone the platform displayed when exporting; empty = journal timezone.")),
            h("div", { class: "field" }, h("label", null, "Fees per contract per side ($)"), feeInput, h("div", { class: "field-hint" }, "Exports carry no fees; applied to every row."))),
          result),
        foot: h("div", { class: "form-actions" }, h("button", { type: "button", class: "btn btn-ghost", onClick: () => closeDrawer() }, "Close"), go) });
    }

    function openTrade(t) {
      const note = h("textarea", { class: "input", rows: 5, placeholder: "What happened? Setup, execution, mistakes, lessons…" });
      note.value = t.note || "";
      const tags = h("input", { class: "input", placeholder: "tags, comma separated (e.g. breakout, fomo, news)", value: (t.tags || []).join(", ") });
      const save = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
        try {
          const upd = await api.put(`/api/journal/trades/${t.id}`, { note: note.value, tags: tags.value.split(",") });
          const i = tradeRows.findIndex((x) => x.id === t.id); if (i >= 0) { tradeRows[i] = upd; trades.update(tradeRows); }
          toast("Note saved", "success"); closeDrawer();
        } catch (e) { toast(e.message, "error"); }
      } }, "Save note");
      const row = (l, v) => h("div", { class: "kv" }, h("dt", null, l), h("dd", null, v));
      openDrawer({ title: `${t.symbol} ${t.side} × ${t.qty}`, width: "520px",
        body: h("div", null,
          h("dl", { class: "kv-list" },
            row("Net P&L", pnl(t.net_pnl)), row("Gross / fees", [fmtSigned(t.gross_pnl, 2), " / ", fmtMoney(t.fees, 2)]),
            row("Entry", `${fmtNum(t.entry_price, 4)} · ${fmtDateTime(t.entry_ts)}`), row("Exit", `${fmtNum(t.exit_price, 4)} · ${fmtDateTime(t.exit_ts)}`),
            row("Points", `${fmtNum(t.points, 4)} × $${fmtNum(t.value_per_point, 2)}/pt`), row("Account", `${t.account_name || t.account_spec} (${t.environment})`),
            row("Source", t.source === "fillpair" ? "Tradovate fill pair" : "FIFO pairing")),
          h("div", { class: "field" }, h("label", null, "Journal note"), note),
          h("div", { class: "field" }, h("label", null, "Tags"), tags)),
        foot: h("div", { class: "form-actions" }, h("button", { type: "button", class: "btn btn-ghost", onClick: () => closeDrawer() }, "Cancel"), save) });
    }

    root.append(
      pageHead("Journal", "Executed trades imported from Tradovate with realised P&L, fees and notes. Reporting per day, week and month; every chart has a table view.", [
        exportLink, csvBtn, importBtn,
      ]),
      h("div", { class: "journal-toolbar" }, rangeSel, periodSel, accountSel, symbolSel, sideSel, h("span", { class: "spacer" }), importInfo),
      card({ title: "Net result" }, hero, heroSub),
      h("div", { class: "kpis" }, Object.values(k).map((x) => x.el)),
      vizCard("P&L per period", "Net realised P&L per day / week / month (switch above). Click a day to open its month in the calendar.", periodChart, periodTable),
      vizCard("Equity curve", "Cumulative net P&L over the selected range.", equityChart, equityTable),
      card({ title: "Calendar", actions: [
        h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: () => shiftMonth(-1) }, "‹"), calTitle,
        h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: () => shiftMonth(1) }, "›"),
        h("button", { type: "button", class: "btn btn-ghost btn-sm viz-table-toggle", onClick: (e) => { const on = calTable.hidden = !calTable.hidden; calBox.hidden = !on; e.target.textContent = on ? "Table" : "Chart"; } }, "Table"),
      ] }, calBox, (() => { calTable.hidden = true; return calTable; })()),
      h("div", { class: "grid grid-2" },
        vizCard("By symbol", null, bySymbol, bySymbolTable),
        vizCard("By account", null, byAccount, byAccountTable),
        vizCard("By weekday", null, byWeekday, byWeekdayTable),
        vizCard(`By hour of day`, "Exit time, journal timezone.", byHour, byHourTable)),
      card({ title: "Trades", hint: "Click a trade to add a note and tags." }, trades.el, h("div", { class: "form-actions", style: "margin-top:8px" }, moreBtn)),
      card({ title: "Imports", hint: "Automatic daily import after the CME close (Settings → General → Trading journal), on demand with Import now, or a CSV export from Tradovate for past days (Import CSV)." }, imports.el),
    );
    load();
    return () => { closeDrawer(); };
  },
};
