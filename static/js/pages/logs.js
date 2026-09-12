/* Logs: event log + signal log, filterable, live via the stream. */
import { h, card, tag, fmtTime, fmtDateTime, pageHead, clear } from "../ui.js";
import { maskAccount } from "../privacy.js";
import { icon } from "../icons.js";
import { store } from "../store.js";
import { actions } from "../actions.js";
import { api } from "../api.js";
import { dataTable } from "../components/table.js";
import { t } from "../i18n.js";

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
function olderLoader(path, renderItem, { label = t("Load older"), params = () => ({}) } = {}) {
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
      if (!page.items.length && !before) note.textContent = t("No older entries.");
      box.append(...page.items.map(renderItem));
      before = page.next_before;
      if (!before) { exhausted = true; btn.hidden = true; note.textContent = page.items.length || box.children.length ? t("End of history.") : t("No older entries."); }
    } catch (e) { note.textContent = e.message; }
    btn.disabled = false;
  }
  function reset() { clear(box); before = null; exhausted = false; btn.hidden = false; note.textContent = ""; }
  return { el: h("div", null, h("div", { class: "form-actions", style: "margin:8px 0" }, btn, note), box), reset };
}

export default {
  title: t("Logs"),
  render(root) {
    let level = "all";
    let q = "";
    const eventBox = h("div", { class: "log-stream" });
    const signalBox = h("div", { class: "log-stream" });
    const search = h("input", { type: "search", placeholder: t("Filter events…"), class: "input-sm", onInput: (e) => { q = e.target.value.toLowerCase(); paintEvents(); } });
    const chips = ["all", "info", "warn", "error"].map((l) => h("button", { type: "button", class: `chip ${l === level ? "active" : ""}`, onClick: (e) => {
      level = l; chips.forEach((c) => c.classList.toggle("active", c === e.currentTarget)); paintEvents();
    } }, l));

    const eventMatch = (e) => (level === "all" || e.level === level) && (!q || String(e.message || "").toLowerCase().includes(q));
    // The live buffers are prepend-only ring buffers (newest first, capped). When
    // the new list is the old one with items added at the top, only those rows
    // are inserted — a signal burst used to rebuild 200 DOM rows per event.
    function incremental(box, prev, list, lineOf, emptyText) {
      if (prev && prev.length && list.length) {
        const n = list.indexOf(prev[0]);                 // where the old top row sits now (identity: same store objects)
        if (n >= 0 && n <= 50) {
          const overlap = Math.min(prev.length, list.length - n);
          let same = true;
          for (let i = 0; i < overlap; i++) if (list[n + i] !== prev[i]) { same = false; break; }
          if (same) {
            if (n === 0 && list.length === prev.length) return;               // nothing changed
            const empty = box.querySelector(".empty-state");
            if (empty) empty.remove();
            for (let i = n - 1; i >= 0; i--) box.prepend(lineOf(list[i]));   // the new rows, newest on top
            while (box.childElementCount > list.length) box.lastElementChild.remove();   // the tail that fell off the buffer
            return;
          }
        }
      }
      clear(box);
      if (!list.length) box.append(h("div", { class: "empty-state" }, emptyText));
      else box.append(...list.map(lineOf));
    }
    let prevEvents = null, prevSignals = null, prevFilter = "";
    function paintEvents() {
      const all = store.get("events") || [];
      const filterKey = `${level}|${q}`;
      const list = all.filter(eventMatch);
      if (filterKey !== prevFilter) { prevEvents = null; prevFilter = filterKey; }
      incremental(eventBox, prevEvents, list, eventLine, t("No events"));
      prevEvents = list;
    }
    function paintSignals() {
      const list = store.get("signals") || [];
      incremental(signalBox, prevSignals, list, signalLine, t("No signals yet"));
      prevSignals = list;
    }

    // --- persisted history (survives restarts/deploys) ---------------------
    const statsBox = h("div", { class: "muted" }, t("Loading…"));
    async function paintStats() {
      try {
        const st = await api.get("/api/history/stats?days=7");
        const tot = st.totals || {};
        clear(statsBox);
        statsBox.append(t("Last 7 days: {r} signals received · {e} executed · ", { r: tot.received || 0, e: tot.executed || 0 }),
          h("span", { class: tot.errors ? "neg" : "" }, t("{n} errors", { n: tot.errors || 0 })), t(" · {s} skipped · {o} orders", { s: tot.skipped || 0, o: tot.orders || 0 }),
          tot.rejected ? h("span", { class: "neg" }, t(" ({n} rejected)", { n: tot.rejected })) : null, ".");
      } catch (e) { statsBox.textContent = ""; }
    }
    const resultFilter = h("select", { class: "input-sm" },
      ["", "ok", "received", "error", "skipped", "test"].map((v) => h("option", { value: v }, v || "any result")));
    const sigSearch = h("input", { type: "search", placeholder: t("Search payload / webhook…"), class: "input-sm" });
    const olderSignals = olderLoader("/api/history/signals", (x) => signalLine(x, true),
      { label: t("Load older signals"), params: () => ({ result: resultFilter.value, q: sigSearch.value }) });
    resultFilter.addEventListener("change", () => olderSignals.reset());
    sigSearch.addEventListener("change", () => olderSignals.reset());

    const orderTable = dataTable({ empty: t("No orders in history"), compact: true, columns: [
      { label: t("Time"), render: (o) => fmtDateTime(o.ts) },
      { label: t("Action"), render: (o) => tag(o.action || "—", (o.action || "").toLowerCase() === "buy" ? "buy" : (o.action || "").toLowerCase() === "sell" ? "sell" : "") },
      { label: t("Symbol"), render: (o) => [o.symbol || "—", o.simulated ? [" ", tag("SIM", "sim")] : null] },
      { label: t("Account"), render: (o) => maskAccount(o.account) || "—" },
      { label: t("Qty"), className: "num", render: (o) => String(o.qty ?? "—") },
      { label: t("Type"), render: (o) => o.order_type || "—" },
      { label: t("Price"), className: "num", render: (o) => String(o.price ?? o.stop_price ?? "—") },
      { label: t("Order id"), render: (o) => String(o.order_id || "—") },
      { label: t("Status"), render: (o) => tag(o.status || "—", (o.status || "").includes("reject") ? "rejected" : "ok") },
    ] });
    const orderRows = [];
    const olderOrders = olderLoader("/api/history/orders", (o) => { orderRows.push(o); orderTable.update(orderRows); return null; },
      { label: t("Load orders") });
    const origReset = olderOrders.reset;
    olderOrders.reset = () => { orderRows.length = 0; orderTable.update([]); origReset(); };

    root.append(
      pageHead(t("Logs"), t("Every event and every received signal, newest first. Updates arrive live; Refresh re-syncs from the server. Signals and orders are kept for 90 days."), [
        h("button", { class: "btn", onClick: () => { actions.refreshLogs(); paintStats(); } }, icon("refresh"), t("Refresh")),
      ]),
      card({ title: t("Event log") }, h("div", { class: "log-toolbar" }, h("div", { class: "chips" }, chips), search), eventBox),
      card({ title: t("Received signals"), hint: t("Result column: received → executed status (ok / skipped / error). Live buffer; older entries load from history below.") },
        statsBox, signalBox,
        h("div", { class: "log-toolbar", style: "margin-top:10px" }, resultFilter, sigSearch), olderSignals.el),
      card({ title: t("Order history"), hint: t("Every order the bridge sent, from the database (survives restarts).") }, orderTable.el, olderOrders.el),
    );
    paintStats();
    const unsubs = [
      store.subscribe("events", paintEvents, { immediate: true }),
      store.subscribe("signals", paintSignals, { immediate: true }),
    ];
    return () => unsubs.forEach((u) => u());
  },
};
