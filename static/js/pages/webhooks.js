/* Webhooks: one URL per strategy; a drawer edits general settings, routed
   accounts, the alert template, test signals, marketplace sharing (admins)
   and the danger zone. Deep link: #/webhooks/<id>. Below the table: the
   signals this area subscribed to on the marketplace. */
import { h, card, tag, toast, confirmDialog, copyText, copyButton, pageHead, clear, fmtDateTime } from "../ui.js";
import { maskAccount } from "../privacy.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { store, can } from "../store.js";
import { actions } from "../actions.js";
import { dataTable } from "../components/table.js";
import { openDrawer, closeDrawer } from "../components/drawer.js";
import { alertMessageTemplate, STRATEGY_LABEL, STRATEGY_OPTIONS, PRESETS, webhookUrl } from "../templates.js";
import { openSubscriptionDrawer } from "./marketplace.js";

const accountKey = (idx, spec) => `${idx}::${spec}`;

async function saveWebhook(id, body) {
  const updated = await api.put(`/api/webhooks/${id}`, body);
  store.update("webhooks", (list) => list.map((w) => (w.id === id ? { ...w, ...updated } : w)));
  return updated;
}

function sharingOf(w) {
  const s = w.sharing || {};
  return { enabled: !!s.enabled, title: s.title || "", description: s.description || "", visibility: s.visibility === "selected" ? "selected" : "all", allowed_user_ids: s.allowed_user_ids || [] };
}

/** Build the drawer for one webhook. */
function webhookDrawer(wh, { navigate }) {
  let w = { ...wh };
  const me = store.get("me") || {};
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
      { label: "Account", render: (a) => h("code", null, maskAccount(a.spec) || "—") },
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
  const forwardCb = h("input", { type: "checkbox", class: "switch" });
  const forwardRow = h("label", { class: `switch-row ${sharingOf(w).enabled ? "" : "hidden"}` },
    h("span", null, "Also forward to marketplace subscribers", h("small", null, "Off by default — a test signal then runs only in your own area.")), forwardCb);
  const presetRow = h("div", { class: "preset-row" }, Object.entries(PRESETS).map(([k, p]) =>
    h("button", { type: "button", class: "chip", onClick: () => { payloadTa.value = JSON.stringify(p.payload, null, 2); } }, p.label)));
  const sendBtn = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
    let payload;
    try { payload = JSON.parse(payloadTa.value); } catch { return toast("Payload is not valid JSON", "error"); }
    if (forwardCb.checked && !(await confirmDialog({ title: "Forward the test signal to subscribers?", body: "It will execute on every subscriber's routed accounts (subject to their own Trading switch).", confirmText: "Send to everyone", danger: true }))) return;
    sendBtn.disabled = true;
    try {
      const r = await api.post(`/api/webhooks/${w.id}/test${forwardCb.checked ? "?subscribers=true" : ""}`, payload);
      testResult.textContent = JSON.stringify(r, null, 2);
      testResult.classList.remove("hidden");
      toast(r.forwarded ? `Signal processed, forwarded to ${r.forwarded} subscriber(s)` : "Signal processed", "success");
      actions.refreshOrders(); actions.refreshLogs(); actions.refreshStatus();
    } catch (e) {
      testResult.textContent = "Error: " + e.message;
      testResult.classList.remove("hidden");
      toast(e.message, "error");
    } finally { sendBtn.disabled = false; }
  } }, icon("send"), "Send test signal");

  // --- Sharing (admins)
  let sharingPane = null;
  if (can(me, "admin")) {
    const sh = sharingOf(w);
    const pubSw = h("input", { type: "checkbox", class: "switch", checked: sh.enabled });
    const titleInp = h("input", { value: sh.title, placeholder: w.name, maxlength: 80 });
    const descTa = h("textarea", { rows: 3, maxlength: 1000, placeholder: "What this signal trades, timeframe, typical size…", style: "font-family:inherit" }, sh.description);
    const visSel = h("select", null, h("option", { value: "all", selected: sh.visibility === "all" }, "Every registered user"), h("option", { value: "selected", selected: sh.visibility === "selected" }, "Only selected users"));
    const userList = h("div", { class: "check-list" }, h("span", { class: "muted" }, "Loading users…"));
    const userBox = h("div", { class: `field ${sh.visibility === "selected" ? "" : "hidden"}` }, h("label", null, "Allowed users"), userList);
    visSel.addEventListener("change", () => userBox.classList.toggle("hidden", visSel.value !== "selected"));
    api.get("/api/users").then((r) => {
      const users = (r.users || r).filter((u) => u.id !== me.id);
      clear(userList);
      if (!users.length) userList.append(h("span", { class: "muted" }, "No other users yet — invite them under Settings → Users."));
      userList.append(users.map((u) => h("label", null, h("input", { type: "checkbox", class: "allow-user", value: String(u.id), checked: sh.allowed_user_ids.includes(u.id) }), u.email)));
    }).catch(() => { clear(userList); userList.append(h("span", { class: "muted" }, "Could not load users.")); });
    const subsTable = dataTable({ empty: "No subscribers yet.", compact: true, columns: [
      { label: "Subscriber", render: (s) => s.email },
      { label: "Status", render: (s) => s.enabled ? tag("on", "on") : tag("off", "off") },
      { label: "Accounts", className: "num", render: (s) => String((s.accounts || []).filter((a) => a.enabled).length) },
      { label: "Since", render: (s) => fmtDateTime(s.created_at) },
      { label: "", render: (s) => h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: async () => {
        if (!(await confirmDialog({ title: `Remove ${s.email}?`, body: "They stop receiving this signal immediately and can subscribe again unless you restrict visibility.", confirmText: "Remove", danger: true }))) return;
        try { await api.del(`/api/webhooks/${w.id}/subscribers/${s.id}`); toast("Subscriber removed", "success"); loadSubs(); }
        catch (e) { toast(e.message, "error"); }
      } }, icon("trash"), "Remove") },
    ] });
    const loadSubs = () => api.get(`/api/webhooks/${w.id}/subscribers`).then((list) => subsTable.update(list)).catch(() => subsTable.update([]));
    loadSubs();
    const shareBtn = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
      shareBtn.disabled = true;
      try {
        const updated = await api.put(`/api/webhooks/${w.id}/sharing`, {
          enabled: pubSw.checked, title: titleInp.value.trim(), description: descTa.value.trim(), visibility: visSel.value,
          allowed_user_ids: [...userList.querySelectorAll(".allow-user:checked")].map((c) => Number(c.value)),
        });
        w = { ...w, ...updated };
        store.update("webhooks", (list) => list.map((x) => (x.id === w.id ? { ...x, ...updated } : x)));
        forwardRow.classList.toggle("hidden", !sharingOf(w).enabled);
        toast(sharingOf(w).enabled ? "Published on the marketplace" : "Sharing saved", "success");
      } catch (e) { toast(e.message, "error"); } finally { shareBtn.disabled = false; }
    } }, icon("share"), "Save sharing");
    sharingPane = h("div", null,
      h("label", { class: "switch-row" }, h("span", null, "Publish on the marketplace", h("small", null, "Other users can subscribe and run this signal on their own accounts. They never see your URL, token or accounts. Unpublishing pauses existing subscriptions.")), pubSw),
      h("div", { class: "grid grid-2", style: "margin-top:14px" },
        h("div", { class: "field" }, h("label", null, "Title shown to subscribers"), titleInp),
        h("div", { class: "field" }, h("label", null, "Visibility"), visSel)),
      h("div", { class: "field" }, h("label", null, "Description"), descTa),
      userBox,
      h("div", { class: "form-actions" }, shareBtn),
      h("h3", null, "Subscribers"),
      subsTable.el);
  }

  // --- Tabs
  const panes = {
    general: h("div", null,
      h("label", { class: "switch-row" }, h("span", null, "Webhook enabled", h("small", null, "Disabled webhooks answer 403 to TradingView (and nothing is forwarded to subscribers).")), enabledSw),
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
      presetRow, payloadTa, forwardRow, h("div", { class: "form-actions" }, sendBtn), testResult),
    sharing: sharingPane,
    danger: h("div", null,
      card({ title: "Regenerate token", cls: "danger", hint: "The current URL stops working immediately — update every TradingView alert that uses it. Subscriptions are unaffected." },
        h("button", { type: "button", class: "btn btn-danger", onClick: async () => {
          if (!(await confirmDialog({ title: "Regenerate token?", body: "The old webhook URL will stop working. Update your TradingView alerts afterwards.", confirmText: "Regenerate", danger: true }))) return;
          try {
            const updated = await api.post(`/api/webhooks/${w.id}/regenerate-token`);
            store.update("webhooks", (list) => list.map((x) => (x.id === w.id ? { ...x, ...updated } : x)));
            w = { ...w, ...updated }; urlCode.textContent = webhookUrl(w.token);
            toast("Token regenerated — copy the new URL", "success");
          } catch (e) { toast(e.message, "error"); }
        } }, icon("key"), "Regenerate token")),
      card({ title: "Delete webhook", cls: "danger", hint: "Removes the webhook, its routing and every marketplace subscription to it. Signals to its URL will be rejected (403)." },
        h("button", { type: "button", class: "btn btn-danger", onClick: async () => {
          if (!(await confirmDialog({ title: `Delete "${w.name}"?`, body: (w.subscriber_count ? `${w.subscriber_count} subscriber(s) lose this signal. ` : "") + "This cannot be undone.", confirmText: "Delete", danger: true }))) return;
          try {
            await api.del(`/api/webhooks/${w.id}`);
            store.update("webhooks", (list) => list.filter((x) => x.id !== w.id));
            toast("Webhook deleted", "success");
            closeDrawer();
          } catch (e) { toast(e.message, "error"); }
        } }, icon("trash"), "Delete webhook"))),
  };
  const tabNames = [["general", "General"], ["accounts", `Accounts (${(w.accounts || []).filter((a) => a.enabled).length})`], ["template", "Alert template"], ["test", "Test signal"]];
  if (sharingPane) tabNames.push(["sharing", `Sharing${w.subscriber_count ? ` (${w.subscriber_count})` : ""}`]);
  tabNames.push(["danger", "Danger zone"]);
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
      w = { ...w, ...updated };
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
        { label: "Name", render: (w) => h("span", { class: "inline-actions" }, h("span", { class: "wh-name" }, w.name),
          sharingOf(w).enabled ? tag(`shared · ${w.subscriber_count || 0}`, "accent") : null) },
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

    // --- Subscriptions (signals from the marketplace)
    const subsTable = dataTable({
      empty: "You haven't subscribed to any published signal.",
      columns: [
        { label: "On", render: (s) => h("input", { type: "checkbox", class: "switch", checked: !!s.enabled, title: "Enable / disable", onChange: async (e) => {
          try { await api.put(`/api/subscriptions/${s.id}`, { enabled: e.target.checked }); toast(e.target.checked ? "Subscription enabled" : "Subscription disabled", "success"); loadSubs(); }
          catch (err) { e.target.checked = !e.target.checked; toast(err.message, "error"); }
        } }) },
        { label: "Signal", render: (s) => h("span", { class: "wh-name" }, s.webhook ? s.webhook.title : s.webhook_id) },
        { label: "Publisher", render: (s) => (s.webhook && s.webhook.publisher_email) || "—" },
        { label: "Strategy", render: (s) => s.webhook ? tag(STRATEGY_LABEL[s.webhook.strategy] || s.webhook.strategy, s.webhook.strategy) : "—" },
        { label: "Accounts", className: "num", render: (s) => String((s.accounts || []).filter((a) => a.enabled).length) },
        { label: "Status", render: (s) => !s.webhook ? tag("unpublished by publisher", "warn") : !s.webhook.webhook_enabled ? tag("paused by publisher", "warn") : s.enabled ? tag("active", "on") : tag("off", "off") },
        { label: "", render: (s) => h("div", { class: "inline-actions" },
          h("button", { type: "button", class: "btn btn-ghost btn-sm", disabled: !s.webhook, onClick: () => openSubscriptionDrawer({ ...(s.webhook || {}), subscription: s }, loadSubs) }, "Manage", icon("chevron")),
          h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: async () => {
            if (!(await confirmDialog({ title: "Unsubscribe?", body: "Future signals from this publisher won't reach your accounts.", confirmText: "Unsubscribe", danger: true }))) return;
            try { await api.del(`/api/subscriptions/${s.id}`); toast("Unsubscribed", "success"); loadSubs(); } catch (e) { toast(e.message, "error"); }
          } }, icon("trash"))) },
      ],
    });
    const loadSubs = () => api.get("/api/subscriptions").then((l) => subsTable.update(l)).catch(() => subsTable.update([]));

    root.append(
      pageHead("Webhooks", "Each strategy gets its own secret URL, its own routed trade accounts and qty multipliers — signals never cross strategies.", [addBtn]),
      card({ title: "Strategy webhooks" }, table.el),
      card({ title: "Subscribed signals", actions: [h("button", { class: "btn btn-ghost btn-sm", onClick: () => navigate("/marketplace") }, icon("store"), "Marketplace")],
        hint: "Signals published by other users that run on your accounts. Your own Trading switch, symbol mapping, alerts and logs apply." }, subsTable.el),
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
    loadSubs();
    if (params.id) {
      // Make sure account data is fresh for the routing table before opening.
      actions.loadTradeAccounts().then(() => { if (!leaving) openFor(params.id); });
    }
    return () => { leaving = true; unsubs.forEach((u) => u()); openId = null; closeDrawer(); };
  },
};
