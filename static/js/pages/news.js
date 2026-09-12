/* Settings → News & Calendar: economic-calendar lock (no new entries around
   high-impact releases), upcoming events with their lock windows, manual events. */
import { h, card, tag, toast, pageHead, clear } from "../ui.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { dataTable } from "../components/table.js";

const fmtTime = (iso) => { const d = new Date(iso); return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }); };
const fmtClock = (iso) => { const d = new Date(iso); return Number.isNaN(d.getTime()) ? "—" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); };
const toLocalInput = (iso) => { const d = new Date(iso); const p = (n) => String(n).padStart(2, "0"); return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`; };

export default {
  title: "News & Calendar",
  gate: "admin",
  render(root) {
    let settings = null;
    const enabled = h("input", { type: "checkbox", class: "switch" });
    const currencies = h("input", { class: "input-sm", placeholder: "USD, EUR", style: "width:180px" });
    const impacts = { High: h("input", { type: "checkbox" }), Medium: h("input", { type: "checkbox" }), Low: h("input", { type: "checkbox" }) };
    const before = h("input", { type: "number", class: "input-sm", min: 0, max: 240, style: "width:90px" });
    const after = h("input", { type: "number", class: "input-sm", min: 0, max: 240, style: "width:90px" });
    const action = h("select", { class: "input-sm" }, h("option", { value: "block" }, "Block new entries"), h("option", { value: "flatten" }, "Block entries and flatten open positions at window start"));
    const alertSw = h("input", { type: "checkbox", class: "switch" });
    const manualBox = h("div", { class: "stack", style: "gap:6px" });
    const manual = [];
    function renderManual() {
      clear(manualBox);
      manual.forEach((m, i) => manualBox.append(h("div", { style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap" },
        h("input", { class: "input-sm", placeholder: "Title (e.g. Powell speech)", value: m.title, style: "min-width:220px", onInput: (e) => { m.title = e.target.value; } }),
        h("input", { type: "datetime-local", class: "input-sm", value: m.at ? toLocalInput(m.at) : "", onInput: (e) => { m.at = e.target.value ? new Date(e.target.value).toISOString() : ""; } }),
        h("button", { type: "button", class: "btn btn-ghost btn-sm", title: "Remove", onClick: () => { manual.splice(i, 1); renderManual(); } }, icon("trash")))));
      manualBox.append(h("button", { type: "button", class: "btn btn-sm", onClick: () => { manual.push({ title: "", at: "" }); renderManual(); } }, icon("plus"), "Add manual event"));
    }
    function fill(s) {
      settings = s;
      enabled.checked = !!s.enabled; currencies.value = (s.currencies || []).join(", ");
      for (const [k, el] of Object.entries(impacts)) el.checked = (s.impacts || []).includes(k);
      before.value = s.before; after.value = s.after; action.value = s.action; alertSw.checked = !!s.alert;
      manual.splice(0, manual.length, ...(s.manual || []).map((m) => ({ ...m }))); renderManual();
    }
    const collect = () => ({ enabled: enabled.checked, currencies: currencies.value, impacts: Object.entries(impacts).filter(([, el]) => el.checked).map(([k]) => k),
      before: Number(before.value) || 0, after: Number(after.value) || 0, action: action.value, alert: alertSw.checked, manual: manual.filter((m) => m.title && m.at) });

    const statusBox = h("div", { class: "callout" }, "Loading…");
    const table = dataTable({ empty: "No events in the selected window for these currencies / impacts.", compact: true, columns: [
      { label: "When", render: (e) => fmtTime(e.at) },
      { label: "Event", render: (e) => [h("strong", null, e.title), e.source === "manual" ? [" ", tag("manual", "accent")] : null] },
      { label: "Cur.", render: (e) => e.currency || "—" },
      { label: "Impact", render: (e) => tag(e.impact, e.impact === "High" ? "error" : e.impact === "Medium" ? "warn" : "") },
      { label: "Forecast / prev.", render: (e) => e.forecast || e.previous ? `${e.forecast || "—"} / ${e.previous || "—"}` : "—" },
      { label: "Lock window", render: (e) => `${fmtClock(e.lock_from)} – ${fmtClock(e.lock_until)}` },
      { label: "", render: (e) => e.active ? tag("locked now", "error") : null },
    ] });
    function paintStatus(st) {
      clear(statusBox);
      statusBox.className = "callout " + (st.active ? "danger" : st.enabled ? "ok" : "");
      if (!st.enabled) statusBox.append("News lock is off. Entries are never blocked by the calendar.");
      else if (st.active) statusBox.append(h("strong", null, "Locked now: "), `${st.active.title} — no new entries until ${fmtClock(st.active.lock_until)}`, st.action === "flatten" ? " (open positions were flattened at the window start)." : ".");
      else if (st.next) statusBox.append(h("strong", null, "Next lock: "), `${st.next.title} — ${fmtTime(st.next.lock_from)} to ${fmtClock(st.next.lock_until)}.`);
      else statusBox.append("Enabled — no matching event in the next 48 hours.");
      const feed = st.feed_events ? `${st.feed_events} events loaded` : "no calendar loaded yet";
      statusBox.append(h("div", { class: "muted", style: "margin-top:6px;font-size:12.5px" }, `Calendar: ${feed}${st.feed_error ? " · feed problem: " + st.feed_error : ""}`));
    }
    async function load() {
      try {
        const r = await api.get("/api/news?hours=168");
        if (!settings) fill(r.settings);
        paintStatus(r.status); table.update(r.events);
      } catch (e) { toast(e.message, "error"); }
    }
    const saveBtn = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
      saveBtn.disabled = true;
      try { const r = await api.put("/api/news/settings", collect()); fill(r.settings); paintStatus(r.status); toast("News lock saved", "success"); await load(); }
      catch (e) { toast(e.message, "error"); } finally { saveBtn.disabled = false; }
    } }, icon("check"), "Save");
    const refreshBtn = h("button", { type: "button", class: "btn", onClick: async () => {
      try { await api.post("/api/news/refresh"); toast("Calendar refreshed", "success"); await load(); } catch (e) { toast(e.message, "error"); }
    } }, icon("refresh"), "Refresh calendar");

    const row = (label, ctrl, hint) => h("div", { class: "field" }, h("label", null, label), ctrl, hint ? h("small", null, hint) : null);
    root.append(
      pageHead("News & Calendar", "No new entries around high-impact releases (FOMC, CPI, NFP …). Closes, stop moves and the copy mirror always run — a position is never left unmanaged. The calendar comes from ForexFactory's weekly feed and is refreshed every six hours."),
      card({ title: "News lock", hint: "Applies to every webhook, Discord signal and marketplace subscription of this workspace. The Simulator is never blocked." },
        h("div", { class: "stack" },
          h("label", { class: "switch-row" }, h("span", null, "News lock enabled", h("small", null, "Off = the calendar is informational only.")), enabled),
          row("Currencies", currencies, "Comma separated. US index and metal futures react to USD releases; add EUR / GBP for FX-driven products."),
          row("Impact levels", h("div", { style: "display:flex;gap:14px;flex-wrap:wrap" }, ...Object.entries(impacts).map(([k, el]) => h("label", { style: "display:flex;gap:6px;align-items:center" }, el, k)))),
          h("div", { style: "display:flex;gap:16px;flex-wrap:wrap" }, row("Minutes before", before), row("Minutes after", after)),
          row("Action", action, "Flatten closes every position on every account of this workspace when the window opens — the same as the SOS button."),
          h("label", { class: "switch-row" }, h("span", null, "Alert when a window opens", h("small", null, "Discord and push.")), alertSw),
          h("h3", null, "Manual events"), h("p", { class: "hint" }, "Speeches, earnings, anything the feed does not carry. Same before / after window."), manualBox,
          h("div", { class: "form-actions" }, saveBtn, refreshBtn))),
      card({ title: "Upcoming events (next 7 days)", hint: "Times in your browser's timezone. Only events matching the currencies and impact levels above are listed." }, statusBox, table.el),
    );
    load();
    const timer = setInterval(load, 60000);
    return () => clearInterval(timer);
  },
};
