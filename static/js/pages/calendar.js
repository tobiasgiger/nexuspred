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
const RANGES = [["2w", "This & next week"], ["today", "Today"], ["7", "Next 7 days"], ["14", "Next 14 days"], ["30", "Next 30 days"], ["past7", "Past 7 days"]];
// Monday 00:00 of the current week (local) … Sunday 23:59 of the following week
function twoWeeks() {
  const now = new Date();
  const mon = new Date(now.getFullYear(), now.getMonth(), now.getDate() - ((now.getDay() + 6) % 7));
  const end = new Date(mon.getFullYear(), mon.getMonth(), mon.getDate() + 14, 0, 0, 0, 0);
  return [mon, new Date(end.getTime() - 1000)];
}

export default {
  title: "Calendar",
  render(root, { navigate }) {
    let range = "2w";
    try { range = localStorage.getItem("fb.calendar.range") || "2w"; } catch { /* ignore */ }
    if (!RANGES.some(([v]) => v === range)) range = "2w";
    const rangeSel = h("select", { class: "input-sm" }, RANGES.map(([v, l]) => h("option", { value: v, selected: v === range }, l)));
    const search = h("input", { class: "input-sm", placeholder: "Search title…", style: "min-width:180px" });
    const relevantSw = h("input", { type: "checkbox", class: "switch" });
    const chipRow = () => h("div", { style: "display:flex;gap:6px;flex-wrap:wrap;align-items:center" });
    const curBox = chipRow();
    const impBox = chipRow();
    const impacts = ["High", "Medium", "Low", "Holiday"];
    let currencies = [];
    const checked = (box) => [...box.querySelectorAll(".chip.active")].map((b) => b.dataset.value);
    // compact toggle chips (one wrapped row) instead of a checkbox list
    const paintChecks = (box, values, selected) => {
      clear(box);
      box.append(...values.map((v) => h("button", { type: "button", class: `chip ${selected.includes(v) ? "active" : ""}`, "data-value": v,
        onClick: (e) => { e.currentTarget.classList.toggle("active"); load(); } }, v)));
    };
    paintChecks(impBox, impacts, ["High", "Medium"]);

    const statusBox = h("div", { class: "callout" }, "Loading…");
    const countEl = h("span", { class: "muted" });
    const table = dataTable({ empty: "No events match the filters in this range.", compact: true, columns: [
      { label: "Time", render: (e) => e._first ? [h("strong", null, fmtDay(e.at)), h("small", { class: "muted", style: "display:block" }, fmtClock(e.at))] : fmtClock(e.at) },
      { label: "Cur.", render: (e) => e.currency || "—" },
      { label: "Impact", render: (e) => tag(e.impact, e.impact === "High" ? "error" : e.impact === "Medium" ? "warn" : e.impact === "Manual" ? "accent" : "") },
      { label: "Event", render: (e) => [h("strong", null, e.title), e.source === "tv" ? h("small", { class: "muted", title: "Preview from TradingView's calendar — replaced by the weekly file's entry on Sunday evening", style: "margin-left:6px" }, "preview") : null] },
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
      const preview = st.preview_events ? `, ${st.preview_events} of them a preview of the coming weeks from TradingView's calendar (replaced by the weekly file each Sunday evening)` : "";
      statusBox.append(h("div", { class: "muted", style: "margin-top:6px;font-size:12.5px" }, `Calendar: ${st.feed_events ? st.feed_events + " events loaded" + span + preview : "no calendar loaded yet"}${st.feed_error ? " · feed problem: " + st.feed_error : ""}. Past weeks are kept for 90 days.`));
    }
    let busy = false;
    async function load() {
      if (busy) return; busy = true;
      try {
        const v = rangeSel.value;
        const params = new URLSearchParams();
        const now = new Date();
        if (v === "2w") { const [a, b] = twoWeeks(); params.set("start", a.toISOString()); params.set("end", b.toISOString()); }
        else if (v === "today") { const e = new Date(now); e.setHours(23, 59, 59, 0); params.set("start", new Date(now.getFullYear(), now.getMonth(), now.getDate()).toISOString()); params.set("end", e.toISOString()); }
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
          const rangeEnd = v === "2w" ? twoWeeks()[1].getTime() : ["today", "past7"].includes(v) ? 0 : Date.now() + Number(v) * 86400e3;
          if (cell && feedTo && rangeEnd > feedTo.getTime()) cell.textContent = `No events match the filters in this range. The calendar currently covers up to ${feedTo.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })} — press Refresh calendar to load further days.`;
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
      pageHead("Calendar", "Economic releases (this week from the weekly file, the coming weeks previewed) plus your manual events, in your browser's timezone. News-lock rules: Settings → News & Calendar.", [
        refreshBtn, h("button", { type: "button", class: "btn btn-ghost", onClick: () => navigate("/settings/news") }, icon("settings"), "Lock settings"),
      ]),
      statusBox,
      card({ title: "Filters" },
        h("div", { style: "display:flex;gap:12px;flex-wrap:wrap;align-items:center" },
          rangeSel, search,
          h("label", { class: "switch-row", style: "padding:0;gap:8px" }, h("span", { class: "muted", style: "font-size:13px" }, "Lock-relevant only"), relevantSw)),
        h("div", { style: "display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px" }, h("span", { class: "muted", style: "font-size:12px;min-width:72px" }, "Currencies"), curBox),
        h("div", { style: "display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:8px" }, h("span", { class: "muted", style: "font-size:12px;min-width:72px" }, "Impact"), impBox)),
      card({ title: "Events", actions: [countEl] }, table.el),
    );
    load();
    const timer = setInterval(load, 60000);
    return () => clearInterval(timer);
  },
};
