/* Publisher controls for a marketplace listing (webhook or copy group) and
   the per-subscriber approve / pause / resume actions. */
import { h, tag, toast } from "../ui.js";
import { api } from "../api.js";
import { store } from "../store.js";
import { t } from "../i18n.js";

/** publisherControls(sharing) → { el, collect } */
export function publisherControls(sh) {
  const maxSubs = h("input", { type: "number", min: 0, max: 10000, step: 1, value: sh.max_subscribers || 0, style: "max-width:160px" });
  const approval = h("input", { type: "checkbox", class: "switch", checked: !!sh.approval });
  const paused = h("input", { type: "checkbox", class: "switch", checked: !!sh.paused });
  const tags = h("input", { type: "text", value: (sh.tags || []).join(", "), placeholder: "nq, scalping, news", maxlength: 120 });
  const pay = ((store.get("status") || {}).payments) || { enabled: false, currency: "usd" };
  const price = h("input", { type: "number", min: 0, step: 0.01, value: sh.price_cents ? (sh.price_cents / 100).toFixed(2) : "", placeholder: "0 = free", style: "max-width:160px" });
  const trialDays = h("input", { type: "number", min: 0, max: 90, step: 1, value: sh.trial_days || 0, style: "max-width:160px" });
  const priceBlock = h("div", { class: "grid grid-2", style: "margin-top:10px" },
    h("div", { class: "field" }, h("label", null, t("Monthly price ({cur})", { cur: (pay.currency || "usd").toUpperCase() })), price, h("div", { class: "field-hint" }, t("0 = free. Charged through the bridge's Stripe account; the operator settles with you."))),
    h("div", { class: "field" }, h("label", null, t("Free trial (days)")), trialDays, h("div", { class: "field-hint" }, t("0 = the bridge default."))));
  if (!pay.enabled) priceBlock.hidden = true;
  const el = h("div", { style: "margin-top:10px" },
    h("h3", null, t("Publisher controls")),
    h("label", { class: "switch-row" }, h("span", null, t("Pause forwarding"), h("small", null, t("Nothing reaches subscribers while paused; the listing stays visible and marked as paused."))), paused),
    h("label", { class: "switch-row" }, h("span", null, t("Approve new subscribers"), h("small", null, t("New subscriptions wait until you approve them in the list below."))), approval),
    h("div", { class: "grid grid-2", style: "margin-top:10px" },
      h("div", { class: "field" }, h("label", null, t("Subscriber limit")), maxSubs, h("div", { class: "field-hint" }, t("0 = unlimited. Existing subscribers are never removed by lowering it."))),
      h("div", { class: "field" }, h("label", null, t("Tags")), tags, h("div", { class: "field-hint" }, t("Up to 5, comma-separated — searchable on the marketplace.")))),
    priceBlock);
  const collect = () => ({ max_subscribers: Number(maxSubs.value) || 0, approval: approval.checked, paused: paused.checked,
    tags: tags.value.split(",").map((x) => x.trim()).filter(Boolean).slice(0, 5),
    ...(pay.enabled ? { price_cents: Math.round((Number(price.value) || 0) * 100), trial_days: Number(trialDays.value) || 0 } : {}) });
  return { el, collect };
}

export function subscriberStatusTag(s) {
  if (s.status === "unpaid") return tag(t("unpaid"), "off");
  if (s.status === "pending") return tag(t("awaiting approval"), "warn");
  if (s.status === "paused") return tag(t("paused by you"), "warn");
  return s.enabled ? tag("on", "on") : tag("off", "off");
}

/** Approve / pause / resume buttons for one subscriber row. `base` = the subscribers URL. */
export function subscriberActions(s, base, reload) {
  const set = async (status) => {
    try { await api.put(`${base}/${s.id}`, { status }); toast(status === "active" ? t("Subscriber active") : t("Subscriber paused"), "success"); reload(); }
    catch (e) { toast(e.message, "error"); }
  };
  if (s.status === "pending") return h("button", { type: "button", class: "btn btn-primary btn-sm", onClick: () => set("active") }, t("Approve"));
  if (s.status === "paused") return h("button", { type: "button", class: "btn btn-sm", onClick: () => set("active") }, t("Resume"));
  return h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: () => set("paused") }, t("Pause"));
}
