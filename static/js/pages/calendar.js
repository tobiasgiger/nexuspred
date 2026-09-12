/* Calendar: every economic-calendar entry (feed + manual events) with filters —
   time range (default: next 7 days), currency, impact, text, lock-relevant only.
   The news-lock rules themselves live under Settings → News & Calendar. */
import { h, card, tag, toast, pageHead, clear } from "../ui.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { dataTable } from "../components/table.js";

const fmtDay = (iso) => new Date(iso).toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" });
const fmtClock = (iso) => { const d = new Date(iso); return Number.isNaN(d.getTime()) ? "—" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); };
const dayKey = (iso) => { const d = new Date(iso); return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`; };
const RANGES = [["today", "Today"], ["3", "Next 3 days"], ["7", "Next 7 days"], ["14", "Next 14 days"], ["30", "Next 30 days"], ["past7", "Past 7 days"]];

export default {
  title: "Calendar",
  render(root, { navigate }) {
    let range = "7";
    try { range = localStorage.getItem("fb.calendar.range") || "7"; } catch { /* ignore */ }
    const rangeSel = h("select", { class: "input-sm" }, RANGES.map(([v, l]) => h("option", { value: v, selected: v === range }, l)));
    const search = h("input", { class: "input-sm", placeholder: "Search title…", style: "min-width:180px" });
    const relevantSw = h("input", { type: "checkbox", class: "switch" });
    const curBox = h("div", { class: "check-list", style: "display:flex;gap:10px;flex-wrap:wrap" });
    const impBox = h("div", { class: "check-list", style: "display:flex;gap:10px;flex-wrap:wrap" });
    const impacts = ["High", "Medium", "Low", "Holiday"];
    let currencies = [];
    const checked = (box) => [...box.querySelectorAll("input:checked")].map((i) => i.value);
    const paintChecks = (box, values, selected) => {
      clear(box);
      box.append(...values.map((v) => h("label", { style: "display:flex;gap:5px;align-items:center" }, h("input", { type: "checkbox", value: v, checked: selected.includes(v), onChange: load }), v)));
    };
    paintChecks(impBox, impacts, ["High", "Medium"]);

    const statusBox = h("div", { class: "callout" }, "Loading…");
    const countEl = h("span", { class: "muted" });
    const table = dataTable({ empty: "No events match the filters in this range.", compact: true, columns: [
      { label: "Time", render: (e) => e._first ? [h("strong", null, fmtDay(e.at)), h("small", { class: "muted", style: "display:block" }, fmtClock(e.at))] : fmtClock(e.at) },
      { label: "Cur.", render: (e) => e.currency || "—" },
      { label: "Impact", render: (e) => tag(e.impact, e.impact === "High" ? "error" : e.impact === "Medium" ? "warn" : e.impact === "Manual" ? "accent" : "") },
      { label: "Event", render: (e) => h("strong", null, e.title) },
      { label: "Forecast", className: "num", render: (e) => e.forecast || "—" },
      { label: "Previous", className: "num", render: (e) => e.previous || "—" },
      { label: "News lock", render: (e) => e.relevant ? [tag(e.active ? "locked now" : "lock window", e.active ? "error" : "accent"), h("small", { class: "muted", style: "display:block" }, `${fmtClock(e.lock_from)} – ${fmtClock(e.lock_until)}`)] : h("span", { class: "muted" }, "—") },
    ] });
    function paintStatus(st) {
      clear(statusBox);
      statusBox.className = "callout " + (st.active ? "danger" : st.enabled ? "ok" : "");
      if (!st.enabled) statusBox.append("News lock is off — entries are informational. Enable it under ", h("a", { href: "#/settings/news" }, "Settings → News & Calendar"), ".");
      else if (st.active) statusBox.append(h("strong", null, "Locked now: "), `${st.active.title} — no new entries until ${fmtClock(st.active.lock_until)}.`);
      else if (st.next) statusBox.append(h("strong", null, "Next lock: "), `${st.next.title} — ${fmtDay(st.next.lock_from)} ${fmtClock(st.next.lock_from)} to ${fmtClock(st.next.lock_until)}.`);
      else statusBox.append("News lock enabled — no matching event in the next 48 hours.");
      const span = st.feed_from ? ` (${new Date(st.feed_from).toLocaleDateString([], { month: "short", day: "numeric" })} – ${new Date(st.feed_to).toLocaleDateString([], { month: "short", day: "numeric" })})` : "";
      statusBox.append(h("div", { class: "muted", style: "margin-top:6px;font-size:12.5px" }, `Calendar: ${st.feed_events ? st.feed_events + " events loaded" + span : "no calendar loaded yet"}${st.feed_error ? " · feed problem: " + st.feed_error : ""}. The feed publishes one week at a time (Monday–Sunday); next week's events appear on Sunday evening. Past weeks are kept for 90 days.`));
    }
    let busy = false;
    async function load() {
      if (busy) return; busy = true;
      try {
        const v = rangeSel.value;
        const params = new URLSearchParams();
        const now = new Date();
        if (v === "today") { const e = new Date(now); e.setHours(23, 59, 59, 0); params.set("start", new Date(now.getFullYear(), now.getMonth(), now.getDate()).toISOString()); params.set("end", e.toISOString()); }
        else if (v === "past7") { params.set("start", new Date(now.getTime() - 7 * 86400e3).toISOString()); params.set("end", now.toISOString()); }
        else params.set("days", v);
        const cur = checked(curBox), imp = checked(impBox);
        if (currencies.length && cur.length && cur.length < currencies.length) params.set("currencies", cur.join(","));
        if (imp.length && imp.length < impacts.length) params.set("impacts", imp.join(","));
        if (search.value.trim()) params.set("q", search.value.trim());
        if (relevantSw.checked) params.set("relevant", "true");
        const r = await api.get(`/api/news/calendar?${params}`);
        if (!currencies.length && r.currencies.length) { currencies = r.currencies; paintChecks(curBox, currencies, currencies.includes("USD") ? ["USD"] : currencies); busy = false; return load(); }
        paintStatus(r.status);
        let last = "";
        const rows = r.events.map((e) => { const k = dayKey(e.at); const first = k !== last; last = k; return { ...e, _first: first }; });
        table.update(rows);
        if (!rows.length) {
          // the feed publishes one week at a time: say so instead of showing an empty week
          const cell = table.tbody.querySelector("td.empty");
          const feedTo = r.status.feed_to ? new Date(r.status.feed_to) : null;
          const rangeEndsAfterFeed = feedTo && !["today", "past7"].includes(v) && (Date.now() + Number(v) * 86400e3 > feedTo.getTime());
          if (cell && rangeEndsAfterFeed) cell.textContent = `No events match the filters in this range. The calendar currently covers up to ${feedTo.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })} — the feed publishes next week's events on Sunday evening; press Refresh calendar then.`;
          else if (cell && r.status.feed_events === 0) cell.textContent = "No calendar loaded yet — press Refresh calendar.";
        }
        countEl.textContent = `${rows.length} event${rows.length === 1 ? "" : "s"} · ${rows.filter((e) => e.relevant).length} lock-relevant`;
        try { localStorage.setItem("fb.calendar.range", v); } catch { /* ignore */ }
      } catch (e) { toast(e.message, "error"); }
      finally { busy = false; }
    }
    rangeSel.addEventListener("change", load); relevantSw.addEventListener("change", load);
    let t = null; search.addEventListener("input", () => { clearTimeout(t); t = setTimeout(load, 250); });
    const refreshBtn = h("button", { type: "button", class: "btn", onClick: async () => {
      try { await api.post("/api/news/refresh"); toast("Calendar refreshed", "success"); await load(); } catch (e) { toast(e.message, "error"); }
    } }, icon("refresh"), "Refresh calendar");

    root.append(
      pageHead("Calendar", "Economic releases from the weekly calendar plus your manual events. Times in your browser's timezone. The rules for the news lock (currencies, impact, window, flatten) are under Settings → News & Calendar.", [
        refreshBtn, h("button", { type: "button", class: "btn btn-ghost", onClick: () => navigate("/settings/news") }, icon("settings"), "Lock settings"),
      ]),
      statusBox,
      card({ title: "Filters" },
        h("div", { style: "display:flex;gap:18px;flex-wrap:wrap;align-items:flex-end" },
          h("div", { class: "field" }, h("label", null, "Range"), rangeSel),
          h("div", { class: "field" }, h("label", null, "Search"), search),
          h("div", { class: "field" }, h("label", null, "Currencies"), curBox),
          h("div", { class: "field" }, h("label", null, "Impact"), impBox),
          h("label", { class: "switch-row", style: "padding:0" }, h("span", null, "Lock-relevant only"), relevantSw))),
      card({ title: "Events", actions: [countEl] }, table.el),
    );
    load();
    const timer = setInterval(load, 60000);
    return () => clearInterval(timer);
  },
};
