/* Logs: event log + signal log, filterable, live via the stream. */
import { h, card, tag, fmtTime, fmtDateTime, pageHead, clear } from "../ui.js";
import { icon } from "../icons.js";
import { store } from "../store.js";
import { actions } from "../actions.js";
import { api } from "../api.js";
import { dataTable } from "../components/table.js";

function eventLine(e) {
  return h("div", { class: "log-line" },
    h("span", { class: "lt" }, fmtTime(e.ts)),
    h("span", { class: `lv ${e.level || ""}` }, (e.level || "").toUpperCase()),
    h("span", { class: "msg" }, e.message || ""));
}

function signalLine(s, full = false) {
  const r = String(s.result || "");
  const tone = r.startsWith("error") ? "error" : r === "skipped" ? "warn" : "info";
  return h("div", { class: "log-line" },
    h("span", { class: "lt" }, full ? fmtDateTime(s.ts) : fmtTime(s.ts)),
    h("span", { class: `lv ${tone}` }, r),
    s.webhook ? h("span", { class: "muted" }, s.webhook, " ") : null,
    h("code", null, JSON.stringify(s.payload)));
}

/* Cursor-paginated "older" section under a live list: one fetch per click,
   appended below what is already shown. */
function olderLoader(path, renderItem, { label = "Load older", params = () => ({}) } = {}) {
  const box = h("div", { class: "log-stream" });
  let before = null, exhausted = false;
  const btn = h("button", { type: "button", class: "btn btn-sm", onClick: load }, icon("refresh"), label);
  const note = h("span", { class: "muted", style: "margin-left:8px" });
  async function load() {
    if (exhausted) return;
    btn.disabled = true;
    try {
      const qs = new URLSearchParams({ limit: "100", ...(before ? { before: String(before) } : {}), ...params() });
      const page = await api.get(`${path}?${qs}`);
      if (!page.items.length && !before) note.textContent = "No older entries.";
      box.append(...page.items.map(renderItem));
      before = page.next_before;
      if (!before) { exhausted = true; btn.hidden = true; note.textContent = page.items.length || box.children.length ? "End of history." : "No older entries."; }
    } catch (e) { note.textContent = e.message; }
    btn.disabled = false;
  }
  function reset() { clear(box); before = null; exhausted = false; btn.hidden = false; note.textContent = ""; }
  return { el: h("div", null, h("div", { class: "form-actions", style: "margin:8px 0" }, btn, note), box), reset };
}

export default {
  title: "Logs",
  render(root) {
    let level = "all";
    let q = "";
    const eventBox = h("div", { class: "log-stream" });
    const signalBox = h("div", { class: "log-stream" });
    const search = h("input", { type: "search", placeholder: "Filter events…", class: "input-sm", onInput: (e) => { q = e.target.value.toLowerCase(); paintEvents(); } });
    const chips = ["all", "info", "warn", "error"].map((l) => h("button", { type: "button", class: `chip ${l === level ? "active" : ""}`, onClick: (e) => {
      level = l; chips.forEach((c) => c.classList.toggle("active", c === e.currentTarget)); paintEvents();
    } }, l));

    function paintEvents() {
      const list = (store.get("events") || []).filter((e) => (level === "all" || e.level === level) && (!q || String(e.message || "").toLowerCase().includes(q)));
      clear(eventBox);
      if (!list.length) eventBox.append(h("div", { class: "empty-state" }, "No events"));
      else eventBox.append(...list.map(eventLine));
    }
    function paintSignals() {
      const list = store.get("signals") || [];
      clear(signalBox);
      if (!list.length) signalBox.append(h("div", { class: "empty-state" }, "No signals yet"));
      else signalBox.append(...list.map(signalLine));
    }

    // --- persisted history (survives restarts/deploys) ---------------------
    const statsBox = h("div", { class: "muted" }, "Loading…");
    async function paintStats() {
      try {
        const st = await api.get("/api/history/stats?days=7");
        const t = st.totals || {};
        clear(statsBox);
        statsBox.append(`Last 7 days: ${t.received || 0} signals received · ${t.executed || 0} executed · `,
          h("span", { class: t.errors ? "neg" : "" }, `${t.errors || 0} errors`), ` · ${t.skipped || 0} skipped · ${t.orders || 0} orders`,
          t.rejected ? h("span", { class: "neg" }, ` (${t.rejected} rejected)`) : null, ".");
      } catch (e) { statsBox.textContent = ""; }
    }
    const resultFilter = h("select", { class: "input-sm" },
      ["", "ok", "received", "error", "skipped", "test"].map((v) => h("option", { value: v }, v || "any result")));
    const sigSearch = h("input", { type: "search", placeholder: "Search payload / webhook…", class: "input-sm" });
    const olderSignals = olderLoader("/api/history/signals", (x) => signalLine(x, true),
      { label: "Load older signals", params: () => ({ result: resultFilter.value, q: sigSearch.value }) });
    resultFilter.addEventListener("change", () => olderSignals.reset());
    sigSearch.addEventListener("change", () => olderSignals.reset());

    const orderTable = dataTable({ empty: "No orders in history", compact: true, columns: [
      { label: "Time", render: (o) => fmtDateTime(o.ts) },
      { label: "Action", render: (o) => tag(o.action || "—", (o.action || "").toLowerCase() === "buy" ? "buy" : (o.action || "").toLowerCase() === "sell" ? "sell" : "") },
      { label: "Symbol", render: (o) => [o.symbol || "—", o.simulated ? [" ", tag("SIM", "sim")] : null] },
      { label: "Account", render: (o) => o.account || "—" },
      { label: "Qty", className: "num", render: (o) => String(o.qty ?? "—") },
      { label: "Type", render: (o) => o.order_type || "—" },
      { label: "Price", className: "num", render: (o) => String(o.price ?? o.stop_price ?? "—") },
      { label: "Order id", render: (o) => String(o.order_id || "—") },
      { label: "Status", render: (o) => tag(o.status || "—", (o.status || "").includes("reject") ? "rejected" : "ok") },
    ] });
    const orderRows = [];
    const olderOrders = olderLoader("/api/history/orders", (o) => { orderRows.push(o); orderTable.update(orderRows); return null; },
      { label: "Load orders" });
    const origReset = olderOrders.reset;
    olderOrders.reset = () => { orderRows.length = 0; orderTable.update([]); origReset(); };

    root.append(
      pageHead("Logs", "Every event and every received signal, newest first. Updates arrive live; Refresh re-syncs from the server. Signals and orders are kept for 90 days.", [
        h("button", { class: "btn", onClick: () => { actions.refreshLogs(); paintStats(); } }, icon("refresh"), "Refresh"),
      ]),
      card({ title: "Event log" }, h("div", { class: "log-toolbar" }, h("div", { class: "chips" }, chips), search), eventBox),
      card({ title: "Received signals", hint: "Result column: received → executed status (ok / skipped / error). Live buffer; older entries load from history below." },
        statsBox, signalBox,
        h("div", { class: "log-toolbar", style: "margin-top:10px" }, resultFilter, sigSearch), olderSignals.el),
      card({ title: "Order history", hint: "Every order the bridge sent, from the database (survives restarts)." }, orderTable.el, olderOrders.el),
    );
    paintStats();
    const unsubs = [
      store.subscribe("events", paintEvents, { immediate: true }),
      store.subscribe("signals", paintSignals, { immediate: true }),
    ];
    return () => unsubs.forEach((u) => u());
  },
};
