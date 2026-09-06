/* Settings pages that are plain forms over /api/settings: general, alerts,
   security, updates, symbol map, account. Each page posts only its own keys. */
import { h, card, tag, toast, confirmDialog, pageHead, fmtDateTime } from "../ui.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { store } from "../store.js";
import { actions } from "../actions.js";
import { settingsForm } from "../components/form.js";
import { dataTable } from "../components/table.js";
import { enablePush, disablePush, currentSubscription, unsupportedReason, isIOS, isStandalone } from "../push.js";

const lead = "Changes are saved per page — only this page's settings are sent.";

export const general = {
  title: "General & Trading",
  render(root) {
    const connectBtn = h("button", { type: "button", class: "btn btn-secondary", onClick: () => actions.connectAll() }, icon("refresh"), "Connect & Verify all");
    const form = settingsForm({
      values: store.get("settings"),
      onSave: (v) => actions.saveSettings(v),
      sections: [
        { title: "Trading rules", fields: [
          { name: "trading_enabled", type: "switch", label: "Trading enabled", hint: "Master kill-switch. Off = signals are logged but never executed." },
          { name: "default_qty", type: "number", label: "Default entry contracts", min: 1, hint: "Used by the Simulator and legacy defaults; each webhook has its own default." },
          { name: "tp_qty", type: "number", label: "Contracts per take-profit", min: 1 },
          { name: "entry_order_type", type: "select", label: "Entry order type", options: ["Market", "Limit"] },
          { name: "tp_order_type", type: "select", label: "Take-profit order type", options: ["Limit", "Market"] },
          { name: "sl_order_type", type: "select", label: "Stop-loss order type", options: ["Stop", "StopLimit"] },
          { name: "breakeven_to_entry", type: "switch", label: "Break-even = entry price", hint: "On a TP1 / “breakeven” move_sl, set the stop to the original entry instead of the signal's new_sl." },
          { name: "allowed_symbols", type: "list", label: "Allowed symbols (unmapped)", placeholder: "MNQ, MES", hint: "Comma separated. Symbols not in the Symbol Mapping are only accepted if their root is listed here." },
        ] },
        { title: "Connection", hint: "Fluxbridge connects to Tradovate with one access token per login (no username/password). Add logins under Tradovate Accounts, then Connect & Verify.", fields: [
          { name: "health_check_interval", type: "number", label: "Health check / token refresh interval (seconds)", min: 0, placeholder: "60 (0 = off)", hint: "How often sessions are verified and tokens renewed ahead of expiry." },
          { name: "pnl_poll_seconds", type: "number", label: "Live P&L refresh (seconds)", min: 0, placeholder: "5 (0 = off)", width: "160px", hint: "How often the Overview's Today's P&L card asks Tradovate for realised / open P&L while a dashboard is open (idle: once a minute)." },
        ], after: h("div", { class: "form-actions" }, connectBtn) },
        { title: "Trading journal", hint: "Executed trades are imported from every enabled Tradovate login once a day (after the CME close) into the Journal page.", fields: [
          { name: "journal_auto_import", type: "switch", label: "Automatic daily import" },
          { name: "journal_import_time", type: "text", label: "Import time (local)", placeholder: "23:30", width: "140px", hint: "HH:MM in the journal timezone. Tradovate's lists cover the current session, so run it after the daily close (23:00 CET)." },
          { name: "journal_timezone", type: "text", label: "Journal timezone", placeholder: "Europe/Zurich", hint: "IANA name; used for the schedule and for day / week / month buckets." },
          { name: "journal_history_days", type: "number", label: "History to import (days)", min: 1, max: 3650, placeholder: "365", width: "160px", hint: "How far back the first import reads Tradovate's Performance report; later runs only fetch what is new." },
          { name: "journal_fee_per_side", type: "number", label: "Fee per contract per side ($)", min: 0, step: 0.01, placeholder: "0", width: "160px", hint: "Applied to trades from reports and CSV exports, which carry no fees (e.g. 1.84 for MNQ at Tradovate)." },
        ] },
      ],
    });
    root.append(pageHead("General & Trading", lead), form.el);
    const unsub = store.subscribe("settings", (s) => { if (!form.isDirty()) form.setValues(s); });
    return () => unsub();
  },
};

export const security = {
  title: "Security",
  render(root) {
    const form = settingsForm({
      values: store.get("settings"),
      onSave: (v) => actions.saveSettings(v),
      sections: [{ title: "Webhook security", hint: "Dashboard access is handled by your account login. This passphrase, if set, is an optional extra check applied to every webhook's JSON body (\"passphrase\": \"…\").", fields: [
        { name: "webhook_passphrase", type: "password", label: "Webhook passphrase (optional)", placeholder: "leave empty to disable" },
      ] }],
    });
    root.append(pageHead("Security", lead), form.el);
    const unsub = store.subscribe("settings", (s) => { if (!form.isDirty()) form.setValues(s); });
    return () => unsub();
  },
};

export const updates = {
  title: "Updates",
  gate: "admin",
  render(root) {
    const status = h("div", { class: "callout" }, "Checking…");
    const applyBtn = h("button", { type: "button", class: "btn btn-update hidden", onClick: async () => {
      if (!(await confirmDialog({ title: "Update now?", body: "Pull the latest version from GitHub and restart the bridge. Open positions are not affected; the dashboard reloads in a few seconds.", confirmText: "Update & restart" }))) return;
      toast("Updating…");
      try {
        const r = await api.post("/api/update/apply");
        toast(r.message, "success");
        setTimeout(() => window.location.reload(), 5000);
      } catch (e) { toast("Update failed: " + e.message, "error"); }
    } }, "Update & restart");
    const checkBtn = h("button", { type: "button", class: "btn btn-secondary", onClick: () => actions.checkUpdate() }, icon("refresh"), "Check now");
    const form = settingsForm({
      values: store.get("settings"),
      onSave: (v) => actions.saveSettings(v),
      sections: [{ title: "Self-updater", fields: [
        { name: "auto_check_updates", type: "switch", label: "Auto-check for updates" },
      ], after: h("div", null, status, h("div", { class: "form-actions" }, checkBtn, applyBtn)) }],
    });
    root.append(pageHead("Updates", "Version status of this bridge and the one-click updater (self-hosted installs)."), form.el);
    const unsubs = [
      store.subscribe("settings", (s) => { if (!form.isDirty()) form.setValues(s); }),
      store.subscribe("update", (u) => {
        if (!u) return;
        if (u.error) { status.className = "callout warn"; status.textContent = "⚠ " + u.error; applyBtn.classList.add("hidden"); return; }
        if (u.update_available) {
          status.className = "callout warn";
          status.textContent = `New version ${u.latest_version} available (current ${u.current_version}) — branch ${u.branch}, ${u.repo}`;
          applyBtn.classList.remove("hidden");
        } else {
          status.className = "callout ok";
          status.textContent = `Up to date (v${u.current_version}) — branch ${u.branch}`;
          applyBtn.classList.add("hidden");
        }
      }, { immediate: true }),
    ];
    if (!store.get("update")) actions.checkUpdate();
    return () => unsubs.forEach((u) => u());
  },
};

/* "Push notifications" block: this device's subscription + every registered device. */
function pushPanel() {
  const status = h("p", { class: "hint" }, "Checking this device…");
  const enableBtn = h("button", { type: "button", class: "btn btn-primary", disabled: true }, icon("bell"), "Enable on this device");
  const disableBtn = h("button", { type: "button", class: "btn btn-ghost", hidden: true }, "Disable on this device");
  const testAllBtn = h("button", { type: "button", class: "btn btn-secondary", hidden: true }, icon("send"), "Test push");
  const actionsRow = h("div", { class: "form-actions", style: "margin-top:12px" }, enableBtn, disableBtn, testAllBtn);
  const table = dataTable({ empty: "No device registered yet.", compact: true, columns: [
    { label: "Device", render: (d) => [h("strong", null, d.device || "Device"), " ", h("span", { class: "muted" }, d.endpoint_host || "")] },
    { label: "Added", render: (d) => fmtDateTime(d.created_at) },
    { label: "Last push", render: (d) => d.last_used_at ? fmtDateTime(d.last_used_at) : "—" },
    { label: "Status", render: (d) => d.failures ? tag(`failing (${d.failures})`, "error") : tag("ok", "ok") },
    { label: "", render: (d) => h("div", { class: "inline-actions" },
      h("button", { type: "button", class: "btn btn-ghost btn-sm", title: "Send a test push to this device", onClick: async () => {
        try { const r = await api.post("/api/push/test", { id: d.id }); toast(r.sent ? "Test push sent" : `Not delivered: ${r.gone ? "device unsubscribed" : "push service rejected it"}`, r.sent ? "success" : "error"); load(); }
        catch (e) { toast(e.message, "error"); }
      } }, icon("send")),
      h("button", { type: "button", class: "btn btn-ghost btn-sm", title: "Remove", onClick: async () => {
        if (!(await confirmDialog({ title: `Remove "${d.device || "this device"}"?`, body: "The device stops receiving push notifications until it is enabled again.", confirmText: "Remove", danger: true }))) return;
        try { await api.del("/api/push/subscribe", { id: d.id }); toast("Device removed", "success"); load(); refreshThisDevice(); } catch (e) { toast(e.message, "error"); }
      } }, icon("trash"))) },
  ] });

  let devices = [];
  async function load() {
    try { devices = await api.get("/api/push/subscriptions"); table.update(devices); testAllBtn.hidden = devices.length === 0; }
    catch (e) { /* ignore */ }
  }
  async function refreshThisDevice() {
    const why = unsupportedReason();
    if (why) {
      status.textContent = why;
      status.className = "hint";
      enableBtn.disabled = true; disableBtn.hidden = true;
      return;
    }
    if (Notification.permission === "denied") {
      status.textContent = "Notifications are blocked for this site. Allow them in the browser / iOS Settings → Notifications and reload.";
      enableBtn.disabled = true; disableBtn.hidden = true;
      return;
    }
    let sub = null;
    try { sub = await currentSubscription(); } catch (e) { sub = null; }
    const known = !!sub && !!(await isKnown(sub.endpoint));
    if (sub && known) {
      status.textContent = `This device receives push notifications${isIOS() && isStandalone() ? " (Home Screen app)" : ""}.`;
      status.className = "hint";
      enableBtn.disabled = true; enableBtn.hidden = true; disableBtn.hidden = false;
    } else {
      status.textContent = sub ? "This device has a browser subscription but is not registered with the bridge — press Enable to register it."
        : (isIOS() ? "Ready. Press Enable and allow notifications — iOS asks once." : "Ready. Press Enable and allow notifications when the browser asks.");
      enableBtn.disabled = false; enableBtn.hidden = false; disableBtn.hidden = true;
    }
  }
  async function isKnown(endpoint) {
    // The bridge never returns full endpoints; compare host + whether *any* device
    // matches this browser's subscription via a HEAD-style check on subscribe.
    try { const r = await api.post("/api/push/known", { endpoint }); return !!r.known; } catch (e) { return false; }
  }
  enableBtn.addEventListener("click", async () => {
    enableBtn.disabled = true; status.textContent = "Asking for permission…";
    try { await enablePush(); toast("Push enabled on this device", "success"); }
    catch (e) { toast(e.message, "error"); status.textContent = e.message; status.className = "hint err"; enableBtn.disabled = false; return; }
    await load(); await refreshThisDevice();
  });
  disableBtn.addEventListener("click", async () => {
    disableBtn.disabled = true;
    try { await disablePush(); toast("Push disabled on this device", "success"); } catch (e) { toast(e.message, "error"); }
    disableBtn.disabled = false;
    await load(); await refreshThisDevice();
  });
  testAllBtn.addEventListener("click", async () => {
    try { const r = await api.post("/api/push/test"); toast(`Test push: ${r.sent} sent, ${r.failed} failed, ${r.gone} removed`, r.sent ? "success" : "error"); load(); }
    catch (e) { toast(e.message, "error"); }
  });
  const el = h("div", null, status, actionsRow,
    h("h3", { style: "margin:18px 0 6px" }, "Registered devices"),
    h("p", { class: "hint" }, "Every device that enabled push for this workspace. On iPhone/iPad open the Home Screen app to enable it; Safari tabs can't receive push."),
    table.el);
  load(); refreshThisDevice();
  return el;
}

export const alerts = {
  title: "Alerts",
  render(root) {
    const testHint = h("span", { class: "save-hint" });
    const testBtn = h("button", { type: "button", class: "btn btn-secondary", onClick: async () => {
      testHint.textContent = "Sending…"; testHint.className = "save-hint";
      try {
        const r = await api.post("/api/alerts/test");
        const on = Object.entries(r.channels || {}).filter(([, v]) => v).map(([k]) => k);
        if (r.status === "none") { testHint.textContent = "No channel enabled — turn on Discord, email or push, save, then test."; testHint.className = "save-hint err"; toast("No alert channel is enabled", "error"); }
        else { testHint.textContent = `Sent to: ${on.join(", ")}. Check that it arrived.`; testHint.className = "save-hint ok"; toast("Test alert sent", "success"); }
      } catch (e) { testHint.textContent = e.message; testHint.className = "save-hint err"; toast(e.message, "error"); }
    } }, icon("bell"), "Send test alert");
    const form = settingsForm({
      values: store.get("settings"),
      onSave: (v) => actions.saveSettings(v),
      sections: [
        { title: "Discord channel", hint: "A Discord webhook URL from the channel's Integrations settings.", fields: [
          { name: "alert_discord_enabled", type: "switch", label: "Discord alerts enabled" },
          { name: "alert_discord_webhook_url", type: "password", label: "Discord webhook URL", placeholder: "https://discord.com/api/webhooks/…" },
          { name: "alert_discord_mention_everyone", type: "switch", label: "Tag @everyone" },
        ] },
        { title: "Email channel", hint: "SMTP, e.g. Gmail with an App Password (not your login password).", fields: [
          { name: "alert_email_enabled", type: "switch", label: "Email alerts enabled" },
          { name: "alert_email_to", type: "email", label: "Notify email", placeholder: "you@example.com" },
          { name: "alert_smtp_host", type: "text", label: "SMTP host", placeholder: "smtp.gmail.com" },
          { name: "alert_smtp_port", type: "number", label: "SMTP port", placeholder: "587", width: "160px" },
          { name: "alert_smtp_username", type: "text", label: "SMTP username", placeholder: "you@gmail.com" },
          { name: "alert_smtp_password", type: "password", label: "SMTP password", placeholder: "App Password" },
        ] },
        { title: "Push notifications", hint: "Notifications on your phone or desktop, even when the dashboard is closed. Works in Chrome/Edge/Firefox and on iPhone/iPad (iOS 16.4+) once the dashboard is added to the Home Screen.", fields: [
          { name: "alert_push_enabled", type: "switch", label: "Push alerts enabled", hint: "Master switch for every registered device" },
        ], after: pushPanel() },
        { title: "Triggers", hint: "Each trigger has its own switch. Trade executed is Discord-only by design among the classic channels; push devices get every trigger.", fields: [
          { name: "alert_on_connection_lost", type: "switch", label: "Connection lost", hint: "Which account + broker — Discord + email" },
          { name: "alert_on_connection_restored", type: "switch", label: "Connection restored", hint: "Discord + email" },
          { name: "alert_on_trade_executed", type: "switch", label: "Trade executed", hint: "Which accounts + strategy — Discord only" },
          { name: "alert_on_webhook_failed", type: "switch", label: "Signal received but not executed", hint: "Webhook failure — Discord + email" },
          { name: "alert_on_discord_lost", type: "switch", label: "Discord listener went offline", hint: "Discord + email" },
          { name: "alert_on_discord_restored", type: "switch", label: "Discord listener came back online", hint: "Discord + email" },
          { name: "alert_on_rollover", type: "switch", label: "Contract rollover due", hint: "A dated contract in the symbol map is near or past its roll date — Discord + email, once per contract" },
          { name: "rollover_warn_days", type: "number", label: "Rollover warning lead time (days)", min: 0, max: 60, placeholder: "10", width: "200px", hint: "Warn this many days before the estimated expiry / first-notice date." },
          { name: "discord_health_grace", type: "number", label: "Discord health grace period (seconds)", min: 15, step: 5, placeholder: "90", width: "200px", hint: "How long the listener may be down before an outage alert fires (avoids alerting on transient reconnects)." },
        ], after: h("div", { class: "form-actions", style: "margin-top:12px" }, testBtn, testHint) },
      ],
    });
    root.append(pageHead("Alerts", "Notify a Discord channel, an email address and/or your phone when something happens. " + lead), form.el);
    const unsub = store.subscribe("settings", (s) => { if (!form.isDirty()) form.setValues(s); });
    return () => unsub();
  },
};

export const symbols = {
  title: "Symbol Mapping",
  render(root) {
    const tbody = h("tbody");
    const row = (tv = "", contract = "") => {
      const tr = h("tr", null,
        h("td", null, h("input", { class: "sm-tv input-sm", value: tv, placeholder: "MNQ1!" })),
        h("td", null, h("input", { class: "sm-contract input-sm", value: contract, placeholder: "MNQU6" })),
        h("td", { style: "width:44px" }, h("button", { type: "button", class: "btn btn-ghost btn-icon", title: "Remove", onClick: () => tr.remove() }, icon("trash"))));
      return tr;
    };
    const paint = (map) => {
      tbody.replaceChildren();
      const entries = Object.entries(map || {});
      tbody.append(...(entries.length ? entries.map(([tv, c]) => row(tv, c)) : [row()]));
    };
    const collect = () => {
      const map = {};
      for (const tr of tbody.querySelectorAll("tr")) {
        const tv = tr.querySelector(".sm-tv").value.trim();
        const c = tr.querySelector(".sm-contract").value.trim();
        if (tv && c) map[tv] = c;
      }
      return map;
    };
    const saveBtn = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
      try { await actions.saveSettings({ symbol_map: collect() }); toast("Symbol mapping saved", "success"); actions.checkRollover(); }
      catch (e) { toast(e.message, "error"); }
    } }, icon("check"), "Save mapping");
    root.append(
      pageHead("Symbol Mapping", "Maps each TradingView symbol to the exact Tradovate contract used for orders. Update the contract after each rollover.", [
        h("button", { type: "button", class: "btn", onClick: () => tbody.append(row()) }, icon("plus"), "Add row"),
      ]),
      card({ title: "Current mapping", hint: "Use a dated contract (e.g. MNQU6); a bare root (e.g. MNQ) also works and auto-picks the front month. Unmapped symbols are only accepted when their root is in Allowed symbols (General & Trading)." },
        h("div", { class: "table-scroll" }, h("table", { class: "data-table" }, h("thead", null, h("tr", null, h("th", null, "TradingView symbol"), h("th", null, "Tradovate contract"), h("th"))), tbody)),
        h("div", { class: "form-actions", style: "margin-top:12px" }, saveBtn)),
    );
    paint((store.get("settings") || {}).symbol_map);
    return () => {};
  },
};

export const account = {
  title: "Account",
  render(root) {
    const me = store.get("me") || {};
    const cur = h("input", { type: "password", autocomplete: "current-password", required: true });
    const nw = h("input", { type: "password", autocomplete: "new-password", required: true, minlength: 8 });
    const nw2 = h("input", { type: "password", autocomplete: "new-password", required: true, minlength: 8 });
    const hint = h("span", { class: "save-hint" });
    const form = h("form", { autocomplete: "off", onSubmit: async (e) => {
      e.preventDefault();
      if (nw.value !== nw2.value) { hint.textContent = "New passwords don't match."; hint.className = "save-hint err"; return; }
      if (nw.value.length < 8) { hint.textContent = "New password must be at least 8 characters."; hint.className = "save-hint err"; return; }
      hint.textContent = "Saving…"; hint.className = "save-hint";
      try {
        await api.post("/api/account/password", { current: cur.value, new: nw.value });
        form.reset(); hint.textContent = "Password changed."; hint.className = "save-hint ok"; toast("Password changed", "success");
      } catch (err) { hint.textContent = err.message; hint.className = "save-hint err"; toast(err.message, "error"); }
    } },
      h("div", { class: "grid grid-2" },
        h("div", { class: "field" }, h("label", null, "Current password"), cur),
        h("div"),
        h("div", { class: "field" }, h("label", null, "New password (min 8 characters)"), nw),
        h("div", { class: "field" }, h("label", null, "Confirm new password"), nw2)),
      h("div", { class: "form-actions" }, h("button", { type: "submit", class: "btn btn-primary" }, "Change password"), hint));
    root.append(
      pageHead("Account", "You're signed in to your own isolated area — token accounts, webhooks, Discord listener, symbol map and logs are private to you."),
      h("div", { class: "grid grid-2" },
        card({ title: "Your account" },
          h("dl", { class: "kv" }, h("dt", null, "Email"), h("dd", null, me.email || "—"), h("dt", null, "Role"), h("dd", null, me.is_admin ? "Administrator" : "User"),
            h("dt", null, "Discord Signals"), h("dd", null, (me.features || {}).discord_signals === false ? "not enabled" : "enabled")),
          h("div", { class: "form-actions", style: "margin-top:14px" }, h("a", { class: "btn btn-ghost", href: "/logout" }, icon("logout"), "Sign out"))),
        card({ title: "Change password" }, form)),
    );
    return () => {};
  },
};
