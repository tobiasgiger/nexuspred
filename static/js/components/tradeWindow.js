/* Trading-window editor (entries only run inside a local time range on given
   weekdays). Shared by the webhook drawer and the subscription controls. */
import { h } from "../ui.js";
import { store } from "../store.js";
import { t } from "../i18n.js";

const DAY_LABELS = () => [["mon", t("Mon")], ["tue", t("Tue")], ["wed", t("Wed")], ["thu", t("Thu")], ["fri", t("Fri")], ["sat", t("Sat")], ["sun", t("Sun")]];

/** tradeWindowEditor(initial, { hint }) → { el, collect } */
export function tradeWindowEditor(initial, { hint = null, title = null } = {}) {
  const tw = { enabled: false, from: "08:00", to: "17:00", tz: "", days: ["mon", "tue", "wed", "thu", "fri"], ...(initial || {}) };
  const uid = `tw-${Math.random().toString(36).slice(2, 8)}`;
  const on = h("input", { type: "checkbox", class: "switch", checked: !!tw.enabled });
  const from = h("input", { type: "time", value: tw.from, style: "width:120px" });
  const to = h("input", { type: "time", value: tw.to, style: "width:120px" });
  const tz = h("input", { type: "text", value: tw.tz || "", placeholder: (store.get("settings") || {}).journal_timezone || "Europe/Zurich", style: "width:200px", list: `${uid}-tz` });
  const days = h("div", { class: "check-list", style: "display:flex;flex-direction:row;flex-wrap:wrap;gap:6px 14px;max-height:none" },
    DAY_LABELS().map(([k, label]) => h("label", { class: "check-item" }, h("input", { type: "checkbox", class: "tw-day", value: k, checked: (tw.days || []).includes(k) }), " ", label)));
  const body = h("div", { class: "grid grid-2", style: "margin-top:10px" },
    h("div", { class: "field" }, h("label", null, t("From")), from),
    h("div", { class: "field" }, h("label", null, t("To")), to, h("div", { class: "field-hint" }, t("End before start = spans midnight (22:00 → 06:00)."))),
    h("div", { class: "field" }, h("label", null, t("Weekdays")), days),
    h("div", { class: "field" }, h("label", null, t("Timezone")), tz, h("datalist", { id: `${uid}-tz` }, ["Europe/Zurich", "Europe/London", "America/New_York", "America/Chicago", "UTC"].map((z) => h("option", { value: z }))),
      h("div", { class: "field-hint" }, t("Empty = the journal timezone (Settings → General)."))));
  const sync = () => body.classList.toggle("hidden", !on.checked);
  on.addEventListener("change", sync); sync();
  const el = h("div", { style: "margin-top:16px" },
    h("label", { class: "switch-row" }, h("span", null, title || t("Trading window"), h("small", null, hint || t("Entries (buy / sell, TS-Hunter signals) only run inside this local time range on these weekdays. Closes, stop moves and management signals always run — an open position is never trapped."))), on),
    body);
  const collect = () => ({ enabled: on.checked, from: from.value || "08:00", to: to.value || "17:00", tz: tz.value.trim(),
    days: [...days.querySelectorAll(".tw-day")].filter((c) => c.checked).map((c) => c.value) });
  return { el, collect };
}
