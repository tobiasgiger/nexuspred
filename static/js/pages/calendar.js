/* Calendar: every economic-calendar entry (feed + manual events) with filters —
   time range (default: next 7 days), currency, impact, text, lock-relevant only.
   The news-lock rules themselves live under Settings → News & Calendar. */
import { h, card, tag, toast, pageHead, clear } from "../ui.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { dataTable } from "../components/table.js";
import { t, locale } from "../i18n.js";

const fmtDay = (iso) => new Date(iso).toLocaleDateString(locale(), { weekday: "long", month: "short", day: "numeric" });
const fmtClock = (iso) => { const d = new Date(iso); return Number.isNaN(d.getTime()) ? "—" : d.toLocaleTimeString(locale(), { hour: "2-digit", minute: "2-digit" }); };
const dayKey = (iso) => { const d = new Date(iso); return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`; };
const RANGES = [["2w", t("This & next week")], ["today", t("Today")], ["7", t("Next 7 days")], ["14", t("Next 14 days")], ["30", t("Next 30 days")], ["past7", t("Past 7 days")]];
// Monday 00:00 of the current week (local) … Sunday 23:59 of the following week
function twoWeeks() {
  const now = new Date();
  const mon = new Date(now.getFullYear(), now.getMonth(), now.getDate() - ((now.getDay() + 6) % 7));
  const end = new Date(mon.getFullYear(), mon.getMonth(), mon.getDate() + 14, 0, 0, 0, 0);
  return [mon, new Date(end.getTime() - 1000)];
}

export default {
  title: t("Calendar"),
  render(root, { navigate }) {
    let range = "2w";
    try { range = localStorage.getItem("fb.calendar.range") || "2w"; } catch { /* ignore */ }
    if (!RANGES.some(([v]) => v === range)) range = "2w";
    const rangeSel = h("select", { class: "input-sm" }, RANGES.map(([v, l]) => h("option", { value: v, selected: v === range }, l)));
    const search = h("input", { class: "input-sm", placeholder: t("Search title…"), style: "min-width:180px" });
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

    const statusBox = h("div", { class: "callout" }, t("Loading…"));
    const countEl = h("span", { class: "muted" });
    const table = dataTable({ empty: t("No events match the filters in this range."), compact: true, columns: [
      { label: t("Time"), render: (e) => e._first ? [h("strong", null, fmtDay(e.at)), h("small", { class: "muted", style: "display:block" }, fmtClock(e.at))] : fmtClock(e.at) },
      { label: t("Cur."), render: (e) => e.currency || "—" },
      { label: t("Impact"), render: (e) => tag(e.impact, e.impact === "High" ? "error" : e.impact === "Medium" ? "warn" : e.impact === "Manual" ? "accent" : "") },
      { label: t("Event"), render: (e) => [h("strong", null, e.title), e.source === "tv" ? h("small", { class: "muted", title: t("Preview from TradingView's calendar — replaced by the weekly file's entry on Sunday evening"), style: "margin-left:6px" }, "preview") : null] },
      { label: t("Forecast"), className: "num", render: (e) => e.forecast || "—" },
      { label: t("Previous"), className: "num", render: (e) => e.previous || "—" },
      { label: t("News lock"), render: (e) => e.relevant ? [tag(e.active ? t("locked now") : t("lock window"), e.active ? "error" : "accent"), h("small", { class: "muted", style: "display:block" }, `${fmtClock(e.lock_from)} – ${fmtClock(e.lock_until)}`)] : h("span", { class: "muted" }, "—") },
    ] });
    function paintStatus(st) {
      clear(statusBox);
      statusBox.className = "callout " + (st.active ? "danger" : st.enabled ? "ok" : "");
      if (!st.enabled) statusBox.append(t("News lock is off — entries are informational. Enable it under "), h("a", { href: "#/settings/news" }, t("Settings → News & Calendar")), ".");
      else if (st.active) statusBox.append(h("strong", null, t("Locked now: ")), t("{title} — no new entries until {until}.", { title: st.active.title, until: fmtClock(st.active.lock_until) }));
      else if (st.next) statusBox.append(h("strong", null, t("Next lock: ")), t("{title} — {day} {from} to {until}.", { title: st.next.title, day: fmtDay(st.next.lock_from), from: fmtClock(st.next.lock_from), until: fmtClock(st.next.lock_until) }));
      else statusBox.append(t("News lock enabled — no matching event in the next 48 hours."));
      const span = st.feed_from ? ` (${new Date(st.feed_from).toLocaleDateString(locale(), { month: "short", day: "numeric" })} – ${new Date(st.feed_to).toLocaleDateString(locale(), { month: "short", day: "numeric" })})` : "";
      const preview = st.preview_events ? t(", {n} of them a preview of the coming weeks from TradingView's calendar (replaced by the weekly file each Sunday evening)", { n: st.preview_events }) : "";
      statusBox.append(h("div", { class: "muted", style: "margin-top:6px;font-size:12.5px" }, (st.feed_events ? t("Calendar: {n} events loaded", { n: st.feed_events }) + span + preview : t("Calendar: no calendar loaded yet")) + (st.feed_error ? t(" · feed problem: ") + st.feed_error : "") + t(". Past weeks are kept for 90 days.")));
    }
    let busy = false, rerun = false;
    async function load() {
      if (busy) { rerun = true; return; } busy = true;
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
          if (cell && feedTo && rangeEnd > feedTo.getTime()) cell.textContent = t("No events match the filters in this range. The calendar currently covers up to {day} — press Refresh calendar to load further days.", { day: feedTo.toLocaleDateString(locale(), { weekday: "short", month: "short", day: "numeric" }) });
          else if (cell && r.status.feed_events === 0) cell.textContent = t("No calendar loaded yet — press Refresh calendar.");
        }
        countEl.textContent = t("{n} events · {r} lock-relevant", { n: rows.length, r: rows.filter((e) => e.relevant).length });
        try { localStorage.setItem("fb.calendar.range", v); } catch { /* ignore */ }
      } catch (e) { toast(e.message, "error"); }
      finally { busy = false; if (rerun) { rerun = false; load(); } }
    }
    rangeSel.addEventListener("change", load); relevantSw.addEventListener("change", load);
    let debounceT = null; search.addEventListener("input", () => { clearTimeout(debounceT); debounceT = setTimeout(load, 250); });
    const refreshBtn = h("button", { type: "button", class: "btn", onClick: async () => {
      try { await api.post("/api/news/refresh"); toast("Calendar refreshed", "success"); await load(); } catch (e) { toast(e.message, "error"); }
    } }, icon("refresh"), t("Refresh calendar"));

    root.append(
      pageHead(t("Calendar"), t("Economic releases (this week from the weekly file, the coming weeks previewed) plus your manual events, in your browser's timezone. News-lock rules: Settings → News & Calendar."), [
        refreshBtn, h("button", { type: "button", class: "btn btn-ghost", onClick: () => navigate("/settings/news") }, icon("settings"), t("Lock settings")),
      ]),
      statusBox,
      card({ title: t("Filters") },
        h("div", { style: "display:flex;gap:12px;flex-wrap:wrap;align-items:center" },
          rangeSel, search,
          h("label", { class: "switch-row", style: "padding:0;gap:8px" }, h("span", { class: "muted", style: "font-size:13px" }, t("Lock-relevant only")), relevantSw)),
        h("div", { style: "display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px" }, h("span", { class: "muted", style: "font-size:12px;min-width:72px" }, t("Currencies")), curBox),
        h("div", { style: "display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:8px" }, h("span", { class: "muted", style: "font-size:12px;min-width:72px" }, t("Impact")), impBox)),
      card({ title: t("Events"), actions: [countEl] }, table.el),
    );
    load();
    const timer = setInterval(load, 60000);
    return () => clearInterval(timer);
  },
};
