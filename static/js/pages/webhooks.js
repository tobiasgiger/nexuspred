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
import { accountKey, routedAccountsTable } from "../components/accounts.js";
import { alertMessageTemplate, STRATEGY_LABEL, STRATEGY_OPTIONS, PRESETS, webhookUrl } from "../templates.js";
import { openSubscriptionDrawer } from "./marketplace.js";
import { sizingOf } from "../sizing.js";
import { t } from "../i18n.js";


/** The sizing rule of a routed account (older entries carry only qty_multiplier). */


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
  const defQtyField = h("div", { class: "field" }, h("label", null, t("Default qty")), defQty, h("div", { class: "field-hint" }, t("Fallback when the alert payload omits qty/contracts.")));
  const tpQtyField = h("div", { class: "field" }, h("label", null, t("Contracts per take-profit")), tpQty, h("div", { class: "field-hint" }, t("Bracket only: size of each TP limit order.")));
  // --- Trading window (entries only)
  const DAY_LABELS = [["mon", t("Mon")], ["tue", t("Tue")], ["wed", t("Wed")], ["thu", t("Thu")], ["fri", t("Fri")], ["sat", t("Sat")], ["sun", t("Sun")]];
  const tw = { enabled: false, from: "08:00", to: "17:00", tz: "", days: ["mon", "tue", "wed", "thu", "fri"], ...(w.trade_window || {}) };
  const twOn = h("input", { type: "checkbox", class: "switch", checked: !!tw.enabled });
  const twFrom = h("input", { type: "time", value: tw.from, style: "width:120px" });
  const twTo = h("input", { type: "time", value: tw.to, style: "width:120px" });
  const twTz = h("input", { type: "text", value: tw.tz || "", placeholder: (store.get("settings") || {}).journal_timezone || "Europe/Zurich", style: "width:200px", list: "tw-tz-list" });
  const twDays = h("div", { class: "check-list", style: "display:flex;flex-direction:row;flex-wrap:wrap;gap:6px 14px;max-height:none" },
    DAY_LABELS.map(([k, label]) => h("label", { class: "check-item" }, h("input", { type: "checkbox", class: "tw-day", value: k, checked: tw.days.includes(k) }), " ", label)));
  const twBody = h("div", { class: "grid grid-2", style: "margin-top:10px" },
    h("div", { class: "field" }, h("label", null, t("From")), twFrom),
    h("div", { class: "field" }, h("label", null, t("To")), twTo, h("div", { class: "field-hint" }, t("End before start = spans midnight (22:00 → 06:00)."))),
    h("div", { class: "field" }, h("label", null, t("Weekdays")), twDays),
    h("div", { class: "field" }, h("label", null, t("Timezone")), twTz, h("datalist", { id: "tw-tz-list" }, ["Europe/Zurich", "Europe/London", "America/New_York", "America/Chicago", "UTC"].map((z) => h("option", { value: z }))),
      h("div", { class: "field-hint" }, t("Empty = the journal timezone (Settings → General)."))));
  const syncWindow = () => twBody.classList.toggle("hidden", !twOn.checked);
  twOn.addEventListener("change", syncWindow); syncWindow();
  const windowBlock = h("div", { style: "margin-top:16px" },
    h("label", { class: "switch-row" }, h("span", null, t("Trading window"), h("small", null, t("Entries (buy / sell, TS-Hunter signals) only run inside this local time range on these weekdays. Closes, stop moves and management signals always run — an open position is never trapped."))), twOn),
    twBody);
  const collectWindow = () => ({ enabled: twOn.checked, from: twFrom.value || "08:00", to: twTo.value || "17:00", tz: twTz.value.trim(),
    days: [...twDays.querySelectorAll(".tw-day")].filter((c) => c.checked).map((c) => c.value) });
  const STRATEGY_BLURB = {
    simple: t("executes buy/sell for the qty in the alert, no TP/SL. close_all flattens the tracked position."),
    bracket: t("market entry + TP1/TP2/TP3 limits + protective stop from the alert; move_sl / trail_active manage the stop."),
    ts_hunter: t("entry sized from risk.value with a stop at sl.value; partial_close_percent slices and full_close, correlated by trade_id."),
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
  const accTable = routedAccountsTable({ known, selected });
  const collectAccounts = accTable.collect;

  // --- Alert template
  const urlCode = h("code", null, webhookUrl(w.token));
  const tmplPre = h("pre", { class: "code" });
  const tmplHint = h("p", { class: "hint" });
  function paintTemplate() {
    const tpl = alertMessageTemplate(stratSel.value);
    tmplPre.textContent = tpl.json;
    tmplHint.textContent = tpl.hint;
  }
  paintTemplate();

  // --- Test
  const payloadTa = h("textarea", { rows: 10, spellcheck: "false" }, JSON.stringify(PRESETS.simple_buy.payload, null, 2));
  const testResult = h("pre", { class: "result-box hidden" });
  const forwardCb = h("input", { type: "checkbox", class: "switch" });
  const forwardRow = h("label", { class: `switch-row ${sharingOf(w).enabled ? "" : "hidden"}` },
    h("span", null, t("Also forward to marketplace subscribers"), h("small", null, t("Off by default — a test signal then runs only in your own area."))), forwardCb);
  const presetRow = h("div", { class: "preset-row" }, Object.entries(PRESETS).map(([k, p]) =>
    h("button", { type: "button", class: "chip", onClick: () => { payloadTa.value = JSON.stringify(p.payload, null, 2); } }, p.label)));
  const sendBtn = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
    let payload;
    try { payload = JSON.parse(payloadTa.value); } catch { return toast(t("Payload is not valid JSON"), "error"); }
    if (forwardCb.checked && !(await confirmDialog({ title: t("Forward the test signal to subscribers?"), body: t("It will execute on every subscriber's routed accounts (subject to their own Trading switch)."), confirmText: t("Send to everyone"), danger: true }))) return;
    sendBtn.disabled = true;
    try {
      const r = await api.post(`/api/webhooks/${w.id}/test${forwardCb.checked ? "?subscribers=true" : ""}`, payload);
      testResult.textContent = JSON.stringify(r, null, 2);
      testResult.classList.remove("hidden");
      toast(r.forwarded ? t("Signal processed, forwarded to {n} subscriber(s)", { n: r.forwarded }) : t("Signal processed"), "success");
      actions.refreshOrders(); actions.refreshLogs(); actions.refreshStatus();
    } catch (e) {
      testResult.textContent = t("Error: ") + e.message;
      testResult.classList.remove("hidden");
      toast(e.message, "error");
    } finally { sendBtn.disabled = false; }
  } }, icon("send"), t("Send test signal"));

  // --- Sharing (admins)
  let sharingPane = null;
  if (can(me, "admin")) {
    const sh = sharingOf(w);
    const pubSw = h("input", { type: "checkbox", class: "switch", checked: sh.enabled });
    const titleInp = h("input", { value: sh.title, placeholder: w.name, maxlength: 80 });
    const descTa = h("textarea", { rows: 3, maxlength: 1000, placeholder: t("What this signal trades, timeframe, typical size…"), style: "font-family:inherit" }, sh.description);
    const visSel = h("select", null, h("option", { value: "all", selected: sh.visibility === "all" }, t("Every registered user")), h("option", { value: "selected", selected: sh.visibility === "selected" }, t("Only selected users")));
    const userList = h("div", { class: "check-list" }, h("span", { class: "muted" }, t("Loading users…")));
    const userBox = h("div", { class: `field ${sh.visibility === "selected" ? "" : "hidden"}` }, h("label", null, t("Allowed users")), userList);
    visSel.addEventListener("change", () => userBox.classList.toggle("hidden", visSel.value !== "selected"));
    api.get("/api/users").then((r) => {
      const users = (r.users || r).filter((u) => u.id !== me.id);
      clear(userList);
      if (!users.length) userList.append(h("span", { class: "muted" }, t("No other users yet — invite them under Settings → Users.")));
      userList.append(users.map((u) => h("label", null, h("input", { type: "checkbox", class: "allow-user", value: String(u.id), checked: sh.allowed_user_ids.includes(u.id) }), u.email)));
    }).catch(() => { clear(userList); userList.append(h("span", { class: "muted" }, t("Could not load users."))); });
    const subsTable = dataTable({ empty: t("No subscribers yet."), compact: true, columns: [
      { label: t("Subscriber"), render: (s) => s.email },
      { label: t("Status"), render: (s) => s.enabled ? tag("on", "on") : tag("off", "off") },
      { label: t("Accounts"), className: "num", render: (s) => String(Array.isArray(s.accounts) ? s.accounts.filter((a) => a.enabled).length : (s.accounts || 0)) },
      { label: t("Since"), render: (s) => fmtDateTime(s.created_at) },
      { label: "", render: (s) => h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: async () => {
        if (!(await confirmDialog({ title: t("Remove {email}?", { email: s.email }), body: t("They stop receiving this signal immediately and can subscribe again unless you restrict visibility."), confirmText: t("Remove"), danger: true }))) return;
        try { await api.del(`/api/webhooks/${w.id}/subscribers/${s.id}`); toast(t("Subscriber removed"), "success"); loadSubs(); }
        catch (e) { toast(e.message, "error"); }
      } }, icon("trash"), t("Remove")) },
    ] });
    const loadSubs = () => api.get(`/api/webhooks/${w.id}/subscribers`).then((list) => subsTable.update(list)).catch((e) => { subsTable.update([]); const c = subsTable.tbody.querySelector("td.empty"); if (c) c.textContent = t("Could not load subscribers: ") + e.message; });
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
        toast(sharingOf(w).enabled ? t("Published on the marketplace") : t("Sharing saved"), "success");
      } catch (e) { toast(e.message, "error"); } finally { shareBtn.disabled = false; }
    } }, icon("share"), t("Save sharing"));
    sharingPane = h("div", null,
      h("label", { class: "switch-row" }, h("span", null, t("Publish on the marketplace"), h("small", null, t("Other users can subscribe and run this signal on their own accounts. They never see your URL, token or accounts. Unpublishing pauses existing subscriptions."))), pubSw),
      h("div", { class: "grid grid-2", style: "margin-top:14px" },
        h("div", { class: "field" }, h("label", null, t("Title shown to subscribers")), titleInp),
        h("div", { class: "field" }, h("label", null, t("Visibility")), visSel)),
      h("div", { class: "field" }, h("label", null, t("Description")), descTa),
      userBox,
      h("div", { class: "form-actions" }, shareBtn),
      h("h3", null, t("Subscribers")),
      subsTable.el);
  }

  // --- Tabs
  const panes = {
    general: h("div", null,
      h("label", { class: "switch-row" }, h("span", null, t("Webhook enabled"), h("small", null, t("Disabled webhooks answer 403 to TradingView (and nothing is forwarded to subscribers)."))), enabledSw),
      h("div", { class: "grid grid-2", style: "margin-top:14px" },
        h("div", { class: "field" }, h("label", null, t("Name")), nameInp),
        h("div", { class: "field" }, h("label", null, t("Strategy")), stratSel),
        defQtyField, tpQtyField),
      blurb, windowBlock),
    accounts: h("div", null,
      h("p", { class: "hint" }, t("Every routed account receives each signal in parallel. Sizing per account — Same: the contracts the signal carries, 1:1. Multiplier: signal × factor (rounded half up, never below 1). Fixed: always this many contracts for the entry; bracket take-profit slices scale proportionally. Max caps the result (0 = no cap).")),
      accTable.el),
    template: h("div", null,
      h("h3", null, t("Webhook URL")),
      h("div", { class: "url-box" }, urlCode, copyButton(() => urlCode.textContent)),
      h("h3", null, t("TradingView alert message")),
      h("p", { class: "hint" }, t("Paste into the alert's Message box (Notifications → Webhook URL = the URL above).")),
      tmplPre, h("div", { class: "form-actions" }, copyButton(() => tmplPre.textContent, "Copy message", "btn btn-secondary btn-sm")), tmplHint),
    test: h("div", null,
      h("div", { class: "callout warn" }, t("Runs the full live pipeline for this webhook: with trading enabled this places REAL orders on the routed accounts.")),
      presetRow, payloadTa, forwardRow, h("div", { class: "form-actions" }, sendBtn), testResult),
    sharing: sharingPane,
    danger: h("div", null,
      card({ title: t("Regenerate token"), cls: "danger", hint: t("The current URL stops working immediately — update every TradingView alert that uses it. Subscriptions are unaffected.") },
        h("button", { type: "button", class: "btn btn-danger", onClick: async () => {
          if (!(await confirmDialog({ title: t("Regenerate token?"), body: t("The old webhook URL will stop working. Update your TradingView alerts afterwards."), confirmText: t("Regenerate"), danger: true }))) return;
          try {
            const updated = await api.post(`/api/webhooks/${w.id}/regenerate-token`);
            store.update("webhooks", (list) => list.map((x) => (x.id === w.id ? { ...x, ...updated } : x)));
            w = { ...w, ...updated }; urlCode.textContent = webhookUrl(w.token);
            toast(t("Token regenerated — copy the new URL"), "success");
          } catch (e) { toast(e.message, "error"); }
        } }, icon("key"), t("Regenerate token"))),
      card({ title: t("Delete webhook"), cls: "danger", hint: t("Removes the webhook, its routing and every marketplace subscription to it. Signals to its URL will be rejected (403).") },
        h("button", { type: "button", class: "btn btn-danger", onClick: async () => {
          if (!(await confirmDialog({ title: t("Delete \"{name}\"?", { name: w.name }), body: (w.subscriber_count ? t("{n} subscriber(s) lose this signal. ", { n: w.subscriber_count }) : "") + t("This cannot be undone."), confirmText: t("Delete"), danger: true }))) return;
          try {
            await api.del(`/api/webhooks/${w.id}`);
            store.update("webhooks", (list) => list.filter((x) => x.id !== w.id));
            toast(t("Webhook deleted"), "success");
            closeDrawer();
          } catch (e) { toast(e.message, "error"); }
        } }, icon("trash"), t("Delete webhook")))),
  };
  const tabNames = [["general", t("General")], ["accounts", `${t("Accounts")} (${(w.accounts || []).filter((a) => a.enabled).length})`], ["template", t("Alert template")], ["test", t("Test signal")]];
  if (sharingPane) tabNames.push(["sharing", `${t("Sharing")}${w.subscriber_count ? ` (${w.subscriber_count})` : ""}`]);
  tabNames.push(["danger", t("Danger zone")]);
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
        trade_window: collectWindow(),
      });
      w = { ...w, ...updated };
      drawer.setTitle(w.name);
      tabsEl.querySelector('[data-tab="accounts"]').textContent = `Accounts (${(w.accounts || []).filter((a) => a.enabled).length})`;
      toast(t("Webhook saved"), "success");
    } catch (e) { toast(e.message, "error"); } finally { saveBtn.disabled = false; }
  } }, icon("check"), t("Save"));
  const drawer = openDrawer({
    title: w.name, body, foot: [saveBtn, h("button", { type: "button", class: "btn btn-ghost", onClick: () => closeDrawer() }, t("Close")),
      h("span", { class: "spacer", style: "flex:1" }), footTag],
    onClose: () => navigate("/webhooks", { replace: true }),
  });
  return drawer;
}

export default {
  title: t("Webhooks"),
  render(root, ctx) {
    const { navigate, params } = ctx;
    const table = dataTable({
      empty: t("No webhooks yet — create one per strategy."),
      onRow: (w) => navigate(`/webhooks/${w.id}`),
      columns: [
        { label: t("On"), render: (w) => h("input", { type: "checkbox", class: "switch", checked: !!w.enabled, title: t("Enable / disable"), onChange: async (e) => {
          try { await saveWebhook(w.id, { enabled: e.target.checked }); toast(e.target.checked ? t("Webhook enabled") : t("Webhook disabled"), "success"); }
          catch (err) { e.target.checked = !e.target.checked; toast(err.message, "error"); }
        } }) },
        { label: t("Name"), render: (w) => h("span", { class: "inline-actions" }, h("span", { class: "wh-name" }, w.name),
          sharingOf(w).enabled ? tag(`${t("shared")} · ${w.subscriber_count || 0}`, "accent") : null) },
        { label: t("Strategy"), render: (w) => tag(STRATEGY_LABEL[w.strategy] || w.strategy, w.strategy) },
        { label: t("Accounts"), className: "num", render: (w) => String((w.accounts || []).filter((a) => a.enabled).length) },
        { label: t("Webhook URL"), render: (w) => h("span", { class: "inline-actions" }, h("code", { class: "wh-url", title: webhookUrl(w.token) }, webhookUrl(w.token)),
          h("button", { type: "button", class: "btn btn-ghost btn-icon btn-sm", title: t("Copy URL"), onClick: async () => toast((await copyText(webhookUrl(w.token))) ? t("Webhook URL copied") : t("Copy failed"), "success") }, icon("copy"))) },
        { label: "", render: (w) => h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: () => navigate(`/webhooks/${w.id}`) }, t("Edit"), icon("chevron")) },
      ],
    });
    const addBtn = h("button", { class: "btn btn-primary", onClick: async () => {
      try {
        const n = (store.get("webhooks") || []).length + 1;
        const wh = await api.post("/api/webhooks", { name: `Strategy ${n}`, strategy: "simple", default_qty: 1, tp_qty: 1 });
        store.update("webhooks", (list) => [...list, wh]);
        toast(t("Webhook created"), "success");
        navigate(`/webhooks/${wh.id}`);
      } catch (e) { toast(e.message, "error"); }
    } }, icon("plus"), t("Add webhook"));

    // --- Subscriptions (signals from the marketplace)
    const subsTable = dataTable({
      empty: t("You haven't subscribed to any published signal."),
      columns: [
        { label: t("On"), render: (s) => h("input", { type: "checkbox", class: "switch", checked: !!s.enabled, title: t("Enable / disable"), onChange: async (e) => {
          try { await api.put(`/api/subscriptions/${s.id}`, { enabled: e.target.checked }); toast(e.target.checked ? t("Subscription enabled") : t("Subscription disabled"), "success"); loadSubs(); }
          catch (err) { e.target.checked = !e.target.checked; toast(err.message, "error"); }
        } }) },
        { label: t("Signal"), render: (s) => h("span", { class: "wh-name" }, s.webhook ? s.webhook.title : s.webhook_id) },
        { label: t("Publisher"), render: (s) => (s.webhook && s.webhook.publisher_email) || "—" },
        { label: t("Strategy"), render: (s) => s.webhook ? tag(STRATEGY_LABEL[s.webhook.strategy] || s.webhook.strategy, s.webhook.strategy) : "—" },
        { label: t("Accounts"), className: "num", render: (s) => String((s.accounts || []).filter((a) => a.enabled).length) },
        { label: t("Status"), render: (s) => !s.webhook ? tag("unpublished by publisher", "warn") : !s.webhook.webhook_enabled ? tag("paused by publisher", "warn") : s.enabled ? tag("active", "on") : tag("off", "off") },
        { label: "", render: (s) => h("div", { class: "inline-actions" },
          h("button", { type: "button", class: "btn btn-ghost btn-sm", disabled: !s.webhook, onClick: () => openSubscriptionDrawer({ ...(s.webhook || {}), subscription: s }, loadSubs) }, t("Manage"), icon("chevron")),
          h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: async () => {
            if (!(await confirmDialog({ title: t("Unsubscribe?"), body: t("Future signals from this publisher won't reach your accounts."), confirmText: t("Unsubscribe"), danger: true }))) return;
            try { await api.del(`/api/subscriptions/${s.id}`); toast(t("Unsubscribed"), "success"); loadSubs(); } catch (e) { toast(e.message, "error"); }
          } }, icon("trash"))) },
      ],
    });
    const loadSubs = () => api.get("/api/subscriptions").then((l) => subsTable.update(l)).catch(() => subsTable.update([]));

    root.append(
      pageHead(t("Webhooks"), t("Each strategy gets its own secret URL, its own routed trade accounts and qty multipliers — signals never cross strategies."), [addBtn]),
      card({ title: t("Strategy webhooks") }, table.el),
      card({ title: t("Subscribed signals"), actions: [h("button", { class: "btn btn-ghost btn-sm", onClick: () => navigate("/marketplace") }, icon("store"), t("Marketplace"))],
        hint: t("Signals published by other users that run on your accounts. Your own Trading switch, symbol mapping, alerts and logs apply.") }, subsTable.el),
      h("div", { class: "callout" }, h("strong", null, "simple"), t(" executes buy/sell for the qty in the alert (no TP/SL) · "),
        h("strong", null, "bracket"), t(" adds TP/SL orders from tp1/tp2/tp3/sl · "), h("strong", null, t("TS-Hunter")), t(" matches the TS-Hunter Pine contract. Accounts come from Settings → Broker Accounts.")),
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
