/* Settings → Payments (admin): the operator's Stripe connection, the admin
   switch for paid listings, the default trial, and every payment record. */
import { h, card, tag, toast, pageHead, fmtDateTime, copyText } from "../ui.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { dataTable } from "../components/table.js";
import { passwordInput } from "../components/form.js";
import { t } from "../i18n.js";

const money = (cents, cur) => `${(Number(cents) / 100).toFixed(2)} ${(cur || "").toUpperCase()}`;
export const PAY_STATUS_TONE = { active: "on", trialing: "on", pending: "", past_due: "warn", canceled: "off", unpaid: "off" };

export default {
  title: t("Payments"),
  gate: "admin",
  render(root) {
    const enabled = h("input", { type: "checkbox", class: "switch" });
    const secret = passwordInput({ placeholder: "sk_live_…", autocomplete: "off" });
    const whsec = passwordInput({ placeholder: "whsec_…", autocomplete: "off" });
    const currency = h("select", null, ["usd", "eur", "chf", "gbp"].map((c) => h("option", { value: c }, c.toUpperCase())));
    const trial = h("input", { type: "number", min: 0, max: 90, step: 1, value: 0, style: "max-width:160px" });
    const hookUrl = h("code", null, `${window.location.origin}/api/payments/webhook`);
    const hint = h("span", { class: "save-hint" });
    const status = h("div", { class: "callout" }, "");
    function paintStatus(c) {
      status.className = `callout ${c.configured ? "ok" : "warn"}`;
      status.textContent = !c.enabled ? t("Paid listings are switched off — every listing behaves as free.")
        : !c.configured ? t("Switched on, but no Stripe secret key yet — listings stay free until it is set.")
        : c.webhook_configured ? t("Live: paid listings require a Stripe subscription; the webhook keeps their status in sync.")
        : t("Live, but no webhook signing secret: payments will never be confirmed. Add the endpoint below in Stripe and paste its signing secret.");
    }
    const saveBtn = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
      hint.textContent = t("Saving…"); hint.className = "save-hint";
      try {
        const c = await api.put("/api/payments/config", { enabled: enabled.checked, stripe_secret_key: secret.input.value, stripe_webhook_secret: whsec.input.value,
          currency: currency.value, trial_days_default: Number(trial.value) || 0 });
        fill(c); hint.textContent = t("Saved."); hint.className = "save-hint ok"; toast(t("Payments settings saved"), "success"); loadRows();
      } catch (e) { hint.textContent = e.message; hint.className = "save-hint err"; toast(e.message, "error"); }
    } }, icon("check"), t("Save"));
    function fill(c) {
      enabled.checked = !!c.enabled; secret.input.value = c.stripe_secret_key || ""; whsec.input.value = c.stripe_webhook_secret || "";
      currency.value = c.currency || "usd"; trial.value = c.trial_days_default || 0; paintStatus(c);
    }
    const rows = dataTable({
      empty: t("No payments yet."),
      columns: [
        { label: t("Subscriber"), render: (p) => p.email || "—" },
        { label: t("Listing"), render: (p) => h("code", null, p.webhook_id) },
        { label: t("Status"), render: (p) => tag(p.status, PAY_STATUS_TONE[p.status] || "") },
        { label: t("Price"), className: "num", render: (p) => money(p.price_cents, p.currency) },
        { label: t("Period ends"), render: (p) => p.current_period_end ? fmtDateTime(p.current_period_end) : "—" },
        { label: t("Trial ends"), render: (p) => p.trial_end ? fmtDateTime(p.trial_end) : "—" },
        { label: t("Stripe"), render: (p) => h("span", { class: "muted", style: "font-size:11px" }, p.stripe_subscription || p.checkout_session || "—") },
        { label: t("Updated"), render: (p) => fmtDateTime(p.updated_at) },
      ],
    });
    const loadRows = () => api.get("/api/payments").then((l) => rows.update(l)).catch((e) => toast(e.message, "error"));
    root.append(
      pageHead(t("Payments"), t("Paid marketplace listings through Stripe Checkout. Money lands in the Stripe account connected here (yours, the operator's); settling with publishers happens outside the bridge. Publishers set a monthly price and an optional trial on their listing; a subscriber gets nothing until Stripe confirms the payment, and stops receiving when it lapses.")),
      card({ title: t("Stripe connection") },
        status,
        h("label", { class: "switch-row" }, h("span", null, t("Paid listings enabled"), h("small", null, t("Off = every listing is free, whatever price it carries."))), enabled),
        h("div", { class: "grid grid-2", style: "margin-top:12px" },
          h("div", { class: "field" }, h("label", null, t("Stripe secret key")), secret.el, h("div", { class: "field-hint" }, t("Developers → API keys. A restricted key (rk_) with Checkout, Customers, Subscriptions and Billing Portal write access is enough."))),
          h("div", { class: "field" }, h("label", null, t("Webhook signing secret")), whsec.el, h("div", { class: "field-hint" }, t("Developers → Webhooks → add endpoint with the URL below; events: checkout.session.completed, customer.subscription.created / updated / deleted, invoice.payment_failed."))),
          h("div", { class: "field" }, h("label", null, t("Currency")), currency),
          h("div", { class: "field" }, h("label", null, t("Default trial (days)")), trial, h("div", { class: "field-hint" }, t("Used when a listing sets no trial of its own. 0 = none.")))),
        h("div", { class: "field" }, h("label", null, t("Webhook endpoint URL")), h("div", { style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap" }, hookUrl,
          h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: async () => toast((await copyText(hookUrl.textContent)) ? t("Copied") : t("Copy failed"), "success") }, icon("copy"), t("Copy")))),
        h("div", { class: "form-actions" }, saveBtn, hint),
        h("p", { class: "hint" }, t("Selling trading signals may be regulated where you and your subscribers live. Check the rules that apply to you before switching this on."))),
      card({ title: t("Payments"), actions: [h("button", { class: "btn btn-ghost btn-sm", onClick: loadRows }, icon("refresh"), t("Refresh"))] }, rows.el),
    );
    api.get("/api/payments/config").then(fill).catch((e) => toast(e.message, "error"));
    loadRows();
    return () => {};
  },
};
