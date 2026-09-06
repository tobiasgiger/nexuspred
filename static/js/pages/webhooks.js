/* Webhooks: one URL per strategy; a drawer edits general settings, routed
   accounts, the alert template and test signals. Deep link: #/webhooks/<id>. */
import { h, card, tag, toast, confirmDialog, copyText, copyButton, pageHead, clear } from "../ui.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { store } from "../store.js";
import { actions } from "../actions.js";
import { dataTable } from "../components/table.js";
import { openDrawer, closeDrawer } from "../components/drawer.js";
import { alertMessageTemplate, STRATEGY_LABEL, STRATEGY_OPTIONS, PRESETS, webhookUrl } from "../templates.js";

const accountKey = (idx, spec) => `${idx}::${spec}`;

async function saveWebhook(id, body) {
  const updated = await api.put(`/api/webhooks/${id}`, body);
  store.update("webhooks", (list) => list.map((w) => (w.id === id ? updated : w)));
  return updated;
}

/** Build the drawer for one webhook. */
function webhookDrawer(wh, { navigate }) {
  let w = { ...wh };
  const known = store.get("tradeAccounts") || [];

  // --- General
  const nameInp = h("input", { value: w.name });
  const stratSel = h("select", null, STRATEGY_OPTIONS.map((o) => h("option", { value: o.value, selected: o.value === w.strategy }, o.label)));
  const enabledSw = h("input", { type: "checkbox", class: "switch", checked: !!w.enabled });
  const defQty = h("input", { type: "number", min: 1, value: w.default_qty ?? 1 });
  const tpQty = h("input", { type: "number", min: 1, value: w.tp_qty ?? 1 });
  const defQtyField = h("div", { class: "field" }, h("label", null, "Default qty"), defQty, h("div", { class: "field-hint" }, "Fallback when the alert payload omits qty/contracts."));
  const tpQtyField = h("div", { class: "field" }, h("label", null, "Contracts per take-profit"), tpQty, h("div", { class: "field-hint" }, "Bracket only: size of each TP limit order."));
  const STRATEGY_BLURB = {
    simple: "executes buy/sell for the qty in the alert, no TP/SL. close_all flattens the tracked position.",
    bracket: "market entry + TP1/TP2/TP3 limits + protective stop from the alert; move_sl / trail_active manage the stop.",
    ts_hunter: "entry sized from risk.value with a stop at sl.value; partial_close_percent slices and full_close, correlated by trade_id.",
  };
  const blurb = h("div", { class: "callout" });
  const footTag = tag(STRATEGY_LABEL[w.strategy] || w.strategy, w.strategy);
  const syncStrategyFields = () => {
    const s = stratSel.value;
    defQtyField.classList.toggle("hidden", s === "ts_hunter");
    tpQtyField.classList.toggle("hidden", s !== "bracket");
    blurb.replaceChildren(h("strong", null, STRATEGY_LABEL[s] || s, ": "), STRATEGY_BLURB[s] || "");
    footTag.textContent = STRATEGY_LABEL[s] || s;
    footTag.className = `tag ${s}`;
    paintTemplate();
  };
  stratSel.addEventListener("change", syncStrategyFields);

  // --- Accounts
  const selected = new Map((w.accounts || []).map((a) => [accountKey(a.token_idx, a.spec), a]));
  const accTable = dataTable({
    empty: "No trade accounts discovered yet — add a login under Settings → Tradovate Accounts and Connect & Verify.",
    columns: [
      { label: "Route", render: (a) => h("input", { type: "checkbox", class: "switch acc-on", checked: !!(selected.get(accountKey(a.token_idx, a.spec)) || {}).enabled, dataset: { key: accountKey(a.token_idx, a.spec) } }) },
      { label: "Login", render: (a) => a.token_name || "—" },
      { label: "Account", render: (a) => h("code", null, a.spec || "—") },
      { label: "Env", render: (a) => tag((a.environment || "—").toUpperCase(), a.environment === "live" ? "live" : "demo") },
      { label: "Status", render: (a) => h("span", { class: a.connected ? "pos" : "muted" }, a.connected ? "connected" : "offline") },
      { label: "Qty ×", render: (a) => h("input", { type: "number", class: "acc-mult input-sm", min: 0.1, step: 0.1, style: "width:80px", value: (selected.get(accountKey(a.token_idx, a.spec)) || {}).qty_multiplier ?? 1, dataset: { key: accountKey(a.token_idx, a.spec) } }) },
    ],
  });
  accTable.update(known);
  const collectAccounts = () => known.map((a) => {
    const key = accountKey(a.token_idx, a.spec);
    const on = accTable.tbody.querySelector(`.acc-on[data-key="${CSS.escape(key)}"]`);
    const mult = accTable.tbody.querySelector(`.acc-mult[data-key="${CSS.escape(key)}"]`);
    return { token_idx: a.token_idx, spec: a.spec, enabled: !!(on && on.checked), qty_multiplier: Number(mult && mult.value) || 1 };
  }).filter((a) => a.enabled);

  // --- Alert template
  const urlCode = h("code", null, webhookUrl(w.token));
  const tmplPre = h("pre", { class: "code" });
  const tmplHint = h("p", { class: "hint" });
  function paintTemplate() {
    const t = alertMessageTemplate(stratSel.value);
    tmplPre.textContent = t.json;
    tmplHint.textContent = t.hint;
  }
  paintTemplate();

  // --- Test
  const payloadTa = h("textarea", { rows: 10, spellcheck: "false" }, JSON.stringify(PRESETS.simple_buy.payload, null, 2));
  const testResult = h("pre", { class: "result-box hidden" });
  const presetRow = h("div", { class: "preset-row" }, Object.entries(PRESETS).map(([k, p]) =>
    h("button", { type: "button", class: "chip", onClick: () => { payloadTa.value = JSON.stringify(p.payload, null, 2); } }, p.label)));
  const sendBtn = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
    let payload;
    try { payload = JSON.parse(payloadTa.value); } catch { return toast("Payload is not valid JSON", "error"); }
    sendBtn.disabled = true;
    try {
      const r = await api.post(`/api/webhooks/${w.id}/test`, payload);
      testResult.textContent = JSON.stringify(r, null, 2);
      testResult.classList.remove("hidden");
      toast("Signal processed", "success");
      actions.refreshOrders(); actions.refreshLogs(); actions.refreshStatus();
    } catch (e) {
      testResult.textContent = "Error: " + e.message;
      testResult.classList.remove("hidden");
      toast(e.message, "error");
    } finally { sendBtn.disabled = false; }
  } }, icon("send"), "Send test signal");

  // --- Tabs
  const panes = {
    general: h("div", null,
      h("label", { class: "switch-row" }, h("span", null, "Webhook enabled", h("small", null, "Disabled webhooks answer 403 to TradingView.")), enabledSw),
      h("div", { class: "grid grid-2", style: "margin-top:14px" },
        h("div", { class: "field" }, h("label", null, "Name"), nameInp),
        h("div", { class: "field" }, h("label", null, "Strategy"), stratSel),
        defQtyField, tpQtyField),
      blurb),
    accounts: h("div", null,
      h("p", { class: "hint" }, "Every routed account receives each signal in parallel; Qty × scales the contracts for that account (1 = as sent)."),
      accTable.el),
    template: h("div", null,
      h("h3", null, "Webhook URL"),
      h("div", { class: "url-box" }, urlCode, copyButton(() => urlCode.textContent)),
      h("h3", null, "TradingView alert message"),
      h("p", { class: "hint" }, "Paste into the alert's Message box (Notifications → Webhook URL = the URL above)."),
      tmplPre, h("div", { class: "form-actions" }, copyButton(() => tmplPre.textContent, "Copy message", "btn btn-secondary btn-sm")), tmplHint),
    test: h("div", null,
      h("div", { class: "callout warn" }, "Runs the full live pipeline for this webhook: with trading enabled this places REAL orders on the routed accounts."),
      presetRow, payloadTa, h("div", { class: "form-actions" }, sendBtn), testResult),
    danger: h("div", null,
      card({ title: "Regenerate token", cls: "danger", hint: "The current URL stops working immediately — update every TradingView alert that uses it." },
        h("button", { type: "button", class: "btn btn-danger", onClick: async () => {
          if (!(await confirmDialog({ title: "Regenerate token?", body: "The old webhook URL will stop working. Update your TradingView alerts afterwards.", confirmText: "Regenerate", danger: true }))) return;
          try {
            const updated = await api.post(`/api/webhooks/${w.id}/regenerate-token`);
            store.update("webhooks", (list) => list.map((x) => (x.id === w.id ? updated : x)));
            w = { ...updated }; urlCode.textContent = webhookUrl(w.token);
            toast("Token regenerated — copy the new URL", "success");
          } catch (e) { toast(e.message, "error"); }
        } }, icon("key"), "Regenerate token")),
      card({ title: "Delete webhook", cls: "danger", hint: "Removes the webhook and its routing. Signals to its URL will be rejected (403)." },
        h("button", { type: "button", class: "btn btn-danger", onClick: async () => {
          if (!(await confirmDialog({ title: `Delete "${w.name}"?`, body: "This cannot be undone.", confirmText: "Delete", danger: true }))) return;
          try {
            await api.del(`/api/webhooks/${w.id}`);
            store.update("webhooks", (list) => list.filter((x) => x.id !== w.id));
            toast("Webhook deleted", "success");
            closeDrawer();
          } catch (e) { toast(e.message, "error"); }
        } }, icon("trash"), "Delete webhook"))),
  };
  const tabNames = [["general", "General"], ["accounts", `Accounts (${(w.accounts || []).filter((a) => a.enabled).length})`], ["template", "Alert template"], ["test", "Test signal"], ["danger", "Danger zone"]];
  const body = h("div");
  const tabsEl = h("div", { class: "tabs" });
  const paneHost = h("div");
  function showTab(name) {
    tabsEl.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    clear(paneHost); paneHost.append(panes[name]);
  }
  tabsEl.append(...tabNames.map(([k, label]) => h("button", { type: "button", dataset: { tab: k }, onClick: () => showTab(k) }, label)));
  body.append(tabsEl, paneHost);
  showTab("general");
  syncStrategyFields();

  const saveBtn = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
    saveBtn.disabled = true;
    try {
      const updated = await saveWebhook(w.id, {
        name: nameInp.value.trim() || "Untitled", enabled: enabledSw.checked, strategy: stratSel.value,
        default_qty: Number(defQty.value) || 1, tp_qty: Number(tpQty.value) || 1, accounts: collectAccounts(),
      });
      w = { ...updated };
      drawer.setTitle(w.name);
      tabsEl.querySelector('[data-tab="accounts"]').textContent = `Accounts (${(w.accounts || []).filter((a) => a.enabled).length})`;
      toast("Webhook saved", "success");
    } catch (e) { toast(e.message, "error"); } finally { saveBtn.disabled = false; }
  } }, icon("check"), "Save");
  const drawer = openDrawer({
    title: w.name, body, foot: [saveBtn, h("button", { type: "button", class: "btn btn-ghost", onClick: () => closeDrawer() }, "Close"),
      h("span", { class: "spacer", style: "flex:1" }), footTag],
    onClose: () => navigate("/webhooks", { replace: true }),
  });
  return drawer;
}

export default {
  title: "Webhooks",
  render(root, ctx) {
    const { navigate, params } = ctx;
    const table = dataTable({
      empty: "No webhooks yet — create one per strategy.",
      onRow: (w) => navigate(`/webhooks/${w.id}`),
      columns: [
        { label: "On", render: (w) => h("input", { type: "checkbox", class: "switch", checked: !!w.enabled, title: "Enable / disable", onChange: async (e) => {
          try { await saveWebhook(w.id, { enabled: e.target.checked }); toast(e.target.checked ? "Webhook enabled" : "Webhook disabled", "success"); }
          catch (err) { e.target.checked = !e.target.checked; toast(err.message, "error"); }
        } }) },
        { label: "Name", render: (w) => h("span", { class: "wh-name" }, w.name) },
        { label: "Strategy", render: (w) => tag(STRATEGY_LABEL[w.strategy] || w.strategy, w.strategy) },
        { label: "Accounts", className: "num", render: (w) => String((w.accounts || []).filter((a) => a.enabled).length) },
        { label: "Webhook URL", render: (w) => h("span", { class: "inline-actions" }, h("code", { class: "wh-url", title: webhookUrl(w.token) }, webhookUrl(w.token)),
          h("button", { type: "button", class: "btn btn-ghost btn-icon btn-sm", title: "Copy URL", onClick: async () => toast((await copyText(webhookUrl(w.token))) ? "Webhook URL copied" : "Copy failed", "success") }, icon("copy"))) },
        { label: "", render: (w) => h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: () => navigate(`/webhooks/${w.id}`) }, "Edit", icon("chevron")) },
      ],
    });
    const addBtn = h("button", { class: "btn btn-primary", onClick: async () => {
      try {
        const n = (store.get("webhooks") || []).length + 1;
        const wh = await api.post("/api/webhooks", { name: `Strategy ${n}`, strategy: "simple", default_qty: 1, tp_qty: 1 });
        store.update("webhooks", (list) => [...list, wh]);
        toast("Webhook created", "success");
        navigate(`/webhooks/${wh.id}`);
      } catch (e) { toast(e.message, "error"); }
    } }, icon("plus"), "Add webhook");

    root.append(
      pageHead("Webhooks", "Each strategy gets its own secret URL, its own routed trade accounts and qty multipliers — signals never cross strategies.", [addBtn]),
      card({ title: "Strategy webhooks" }, table.el),
      h("div", { class: "callout" }, h("strong", null, "simple"), " executes buy/sell for the qty in the alert (no TP/SL) · ",
        h("strong", null, "bracket"), " adds TP/SL orders from tp1/tp2/tp3/sl · ", h("strong", null, "TS-Hunter"), " matches the TS-Hunter Pine contract. Accounts come from Settings → Tradovate Accounts."),
    );

    let openId = null;
    let leaving = false;  // set while the page is torn down: the drawer must not navigate
    const openFor = (id) => {
      const w = (store.get("webhooks") || []).find((x) => x.id === id);
      if (!w) { if (id) navigate("/webhooks", { replace: true }); return; }
      if (openId === id) return;
      openId = id;
      webhookDrawer(w, { navigate: (p, o) => {
        openId = null;
        if (!leaving) navigate(p, o);
      } });
    };
    const unsubs = [
      store.subscribe("webhooks", (list) => table.update(list || []), { immediate: true }),
      store.subscribe("route", (r) => { if (r && r.path.startsWith("/webhooks")) { if (r.params.id) openFor(r.params.id); } }),
    ];
    if (params.id) {
      // Make sure account data is fresh for the routing table before opening.
      actions.loadTradeAccounts().then(() => { if (!leaving) openFor(params.id); });
    }
    return () => { leaving = true; unsubs.forEach((u) => u()); openId = null; closeDrawer(); };
  },
};
