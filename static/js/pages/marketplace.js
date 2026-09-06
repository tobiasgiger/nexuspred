/* Marketplace: signals other users published; subscribe with your own accounts. */
import { h, card, tag, toast, confirmDialog, pageHead, clear } from "../ui.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { store } from "../store.js";
import { actions } from "../actions.js";
import { dataTable } from "../components/table.js";
import { openDrawer, closeDrawer } from "../components/drawer.js";
import { STRATEGY_LABEL } from "../templates.js";

const accountKey = (idx, spec) => `${idx}::${spec}`;

/**
 * Subscribe / edit-subscription drawer.
 * item: {publisher_area_id, webhook_id, title, description, strategy, publisher_email, subscription|null}
 * onDone(): called after save / unsubscribe.
 */
export function openSubscriptionDrawer(item, onDone) {
  const sub = item.subscription || null;
  const known = store.get("tradeAccounts") || [];
  const selected = new Map(((sub && sub.accounts) || []).map((a) => [accountKey(a.token_idx, a.spec), a]));
  const enabledSw = h("input", { type: "checkbox", class: "switch", checked: sub ? !!sub.enabled : true });
  const accTable = dataTable({
    empty: "No trade accounts discovered yet — add a login under Settings → Tradovate Accounts and Connect & Verify.",
    columns: [
      { label: "Route", render: (a) => h("input", { type: "checkbox", class: "switch acc-on", checked: !!(selected.get(accountKey(a.token_idx, a.spec)) || {}).enabled, dataset: { key: accountKey(a.token_idx, a.spec) } }) },
      { label: "Login", render: (a) => a.token_name || "—" },
      { label: "Account", render: (a) => h("code", null, a.spec || "—") },
      { label: "Env", render: (a) => tag((a.environment || "—").toUpperCase(), a.environment === "live" ? "live" : "demo") },
      { label: "Qty ×", render: (a) => h("input", { type: "number", class: "acc-mult input-sm", min: 0.1, step: 0.1, style: "width:80px", value: (selected.get(accountKey(a.token_idx, a.spec)) || {}).qty_multiplier ?? 1, dataset: { key: accountKey(a.token_idx, a.spec) } }) },
    ],
  });
  accTable.update(known);
  const collect = () => known.map((a) => {
    const key = accountKey(a.token_idx, a.spec);
    const on = accTable.tbody.querySelector(`.acc-on[data-key="${CSS.escape(key)}"]`);
    const mult = accTable.tbody.querySelector(`.acc-mult[data-key="${CSS.escape(key)}"]`);
    return { token_idx: a.token_idx, spec: a.spec, enabled: !!(on && on.checked), qty_multiplier: Number(mult && mult.value) || 1 };
  }).filter((a) => a.enabled);

  const saveBtn = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
    const body = { enabled: enabledSw.checked, accounts: collect() };
    if (!body.accounts.length && enabledSw.checked) {
      if (!(await confirmDialog({ title: "No accounts routed", body: "The subscription will be active but trade on no account. Save anyway?", confirmText: "Save" }))) return;
    }
    saveBtn.disabled = true;
    try {
      if (sub) await api.put(`/api/subscriptions/${sub.id}`, body);
      else await api.post(`/api/marketplace/${item.publisher_area_id}/${item.webhook_id}/subscribe`, body);
      toast(sub ? "Subscription saved" : `Subscribed to ${item.title}`, "success");
      closeDrawer();
      if (onDone) onDone();
    } catch (e) { toast(e.message, "error"); } finally { saveBtn.disabled = false; }
  } }, icon("check"), sub ? "Save" : "Subscribe");
  const unsubBtn = sub ? h("button", { type: "button", class: "btn btn-danger", onClick: async () => {
    if (!(await confirmDialog({ title: `Unsubscribe from "${item.title}"?`, body: "Future signals from this publisher won't reach your accounts. Open positions are not touched.", confirmText: "Unsubscribe", danger: true }))) return;
    try { await api.del(`/api/subscriptions/${sub.id}`); toast("Unsubscribed", "success"); closeDrawer(); if (onDone) onDone(); }
    catch (e) { toast(e.message, "error"); }
  } }, icon("trash"), "Unsubscribe") : null;

  openDrawer({
    title: item.title,
    body: [
      h("div", { class: "callout" },
        h("div", null, h("strong", null, "Publisher: "), item.publisher_email || "—", " · ", h("strong", null, "Strategy: "), tag(STRATEGY_LABEL[item.strategy] || item.strategy, item.strategy)),
        item.description ? h("div", { style: "margin-top:6px;white-space:pre-line" }, item.description) : null),
      h("label", { class: "switch-row" }, h("span", null, "Subscription active", h("small", null, "Off = signals from this publisher are ignored for your accounts. Your own Trading switch applies as well.")), enabledSw),
      h("h3", null, "Trade on my accounts"),
      h("p", { class: "hint" }, "Signals execute on every routed account below, in parallel, scaled by Qty ×. The publisher never sees your accounts."),
      accTable.el,
    ],
    foot: [saveBtn, h("button", { type: "button", class: "btn btn-ghost", onClick: () => closeDrawer() }, "Close"), h("span", { style: "flex:1" }), unsubBtn],
  });
}

export default {
  title: "Marketplace",
  render(root, { navigate }) {
    const grid = h("div", { class: "mk-grid" });
    const status = h("p", { class: "hint" }, "Loading…");

    async function load() {
      try {
        const items = await api.get("/api/marketplace");
        clear(grid);
        status.textContent = items.length ? "" : "";
        if (!items.length) {
          grid.append(h("div", { class: "card", style: "grid-column:1/-1" }, h("div", { class: "empty-state" }, icon("store"), h("div", null, "No signals are published for your account right now."))));
          return;
        }
        for (const it of items) {
          const sub = it.subscription;
          const state = !sub ? tag("not subscribed") : !it.webhook_enabled ? tag("paused by publisher", "warn") : sub.enabled ? tag("subscribed · on", "on") : tag("subscribed · off", "off");
          grid.append(h("div", { class: "card mk-card" },
            h("div", { class: "mk-title" }, h("strong", null, it.title), tag(STRATEGY_LABEL[it.strategy] || it.strategy, it.strategy)),
            h("div", { class: "mk-desc" }, it.description || "No description."),
            h("div", { class: "mk-meta" }, icon("user"), it.publisher_email || "—", "·", icon("users"), `${it.subscriber_count} subscriber${it.subscriber_count === 1 ? "" : "s"}`,
              it.visibility === "selected" ? ["·", tag("invite-only", "accent")] : null),
            h("div", { class: "mk-foot" }, state,
              h("div", { class: "inline-actions" },
                sub ? h("input", { type: "checkbox", class: "switch", checked: !!sub.enabled, title: "Enable / disable", onChange: async (e) => {
                  try { await api.put(`/api/subscriptions/${sub.id}`, { enabled: e.target.checked }); toast(e.target.checked ? "Subscription enabled" : "Subscription disabled", "success"); load(); }
                  catch (err) { e.target.checked = !e.target.checked; toast(err.message, "error"); }
                } }) : null,
                h("button", { type: "button", class: `btn btn-sm ${sub ? "" : "btn-primary"}`, onClick: () => openSubscriptionDrawer(it, load) }, sub ? "Manage" : "Subscribe")))));
        }
      } catch (e) {
        status.textContent = e.message;
      }
    }

    root.append(
      pageHead("Marketplace", "Signals other users have published. Subscribe to run them on your own trade accounts — with your own quantity multiplier, your own Trading switch and your own logs and alerts. Publishers never see your accounts.", [
        h("button", { class: "btn", onClick: load }, icon("refresh"), "Refresh"),
        h("button", { class: "btn btn-ghost", onClick: () => navigate("/webhooks") }, "My subscriptions", icon("chevron")),
      ]),
      status, grid,
    );
    if (!(store.get("tradeAccounts") || []).length) actions.loadTradeAccounts();
    load();
    return () => {};
  },
};
