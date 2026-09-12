/* Settings pages that are plain forms over /api/settings: general, alerts,
   security, updates, symbol map, account. Each page posts only its own keys. */
import { h, card, tag, toast, confirmDialog, pageHead, fmtDateTime, clear } from "../ui.js";
import { maskAccount } from "../privacy.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { store } from "../store.js";
import { actions } from "../actions.js";
import { settingsForm } from "../components/form.js";
import { dataTable } from "../components/table.js";
import { enablePush, disablePush, currentSubscription, unsupportedReason, isIOS, isStandalone } from "../push.js";
import { openDrawer, closeDrawer } from "../components/drawer.js";
import { copyText } from "../ui.js";
import { t, LANGUAGES } from "../i18n.js";

const lead = () => t("Changes are saved per page — only this page's settings are sent.");

export const general = {
  title: t("General & Trading"),
  render(root) {
    const connectBtn = h("button", { type: "button", class: "btn btn-secondary", onClick: () => actions.connectAll() }, icon("refresh"), t("Connect & Verify all"));
    const form = settingsForm({
      values: store.get("settings"),
      onSave: (v) => actions.saveSettings(v),
      sections: [
        { title: t("Display"), fields: [
          { name: "ui_language", type: "select", label: t("Language"), options: LANGUAGES.map(([v, l]) => ({ value: v, label: v === "auto" ? t("Browser default") : l })),
            hint: t("Browser default follows your browser's language (German or English); the page reloads after a change. Sign-in pages use the same choice.") },
        ] },
        { title: t("Trading rules"), fields: [
          { name: "trading_enabled", type: "switch", label: t("Trading enabled"), hint: t("Master kill-switch. Off = signals are logged but never executed.") },
          { name: "default_qty", type: "number", label: t("Default entry contracts"), min: 1, hint: t("Used by the Simulator and legacy defaults; each webhook has its own default.") },
          { name: "tp_qty", type: "number", label: t("Contracts per take-profit"), min: 1 },
          { name: "entry_order_type", type: "select", label: t("Entry order type"), options: ["Market", "Limit"] },
          { name: "tp_order_type", type: "select", label: t("Take-profit order type"), options: ["Limit", "Market"] },
          { name: "sl_order_type", type: "select", label: t("Stop-loss order type"), options: ["Stop", "StopLimit"] },
          { name: "breakeven_to_entry", type: "switch", label: t("Break-even = entry price"), hint: t("On a TP1 / “breakeven” move_sl, set the stop to the original entry instead of the signal's new_sl.") },
          { name: "allowed_symbols", type: "list", label: t("Allowed symbols (unmapped)"), placeholder: t("MNQ, MES"), hint: t("Comma separated. Symbols not in the Symbol Mapping are only accepted if their root is listed here.") },
        ] },
        { title: t("Connection"), hint: t("Fluxbridge connects to each broker login separately — Tradovate with an access token (no username/password), Rithmic and ProjectX with credentials. Add logins under Broker Accounts, then Connect & Verify."), fields: [
          { name: "health_check_interval", type: "number", label: t("Health check / token refresh interval (seconds)"), min: 0, placeholder: t("60 (0 = off)"), hint: t("How often sessions are verified and tokens renewed ahead of expiry.") },
          { name: "pnl_poll_seconds", type: "number", label: t("Live P&L refresh (seconds)"), min: 0, placeholder: t("5 (0 = off)"), width: "160px", hint: t("How often the Overview's Today's P&L card asks the broker for realised / open P&L while a dashboard is open (idle: once a minute).") },
        ], after: h("div", { class: "form-actions" }, connectBtn) },
        { title: t("Trading journal"), hint: t("Executed trades are imported from every enabled Tradovate login once a day (after the CME close) into the Journal page. Rithmic and ProjectX logins are not imported yet — use the CSV import on the Journal page."), fields: [
          { name: "journal_auto_import", type: "switch", label: t("Automatic daily import") },
          { name: "journal_import_time", type: "text", label: t("Import time (local)"), placeholder: "23:30", width: "140px", hint: t("HH:MM in the journal timezone. Tradovate's lists cover the current session, so run it after the daily close (23:00 CET).") },
          { name: "journal_timezone", type: "text", label: t("Journal timezone"), placeholder: t("Europe/Zurich"), hint: t("IANA name; used for the schedule and for day / week / month buckets.") },
          { name: "journal_history_days", type: "number", label: t("History to import (days)"), min: 1, max: 3650, placeholder: "365", width: "160px", hint: t("How far back the first import reads Tradovate's Performance report; later runs only fetch what is new.") },
          { name: "journal_fee_per_side", type: "number", label: t("Fee per contract per side ($)"), min: 0, step: 0.01, placeholder: "0", width: "160px", hint: t("Applied to trades from reports and CSV exports, which carry no fees (e.g. 1.84 for MNQ at Tradovate).") },
        ] },
      ],
    });
    root.append(pageHead(t("General & Trading"), lead()), form.el);
    const unsub = store.subscribe("settings", (s) => { if (!form.isDirty()) form.setValues(s); });
    return () => unsub();
  },
};

export const security = {
  title: t("Security"),
  render(root) {
    const form = settingsForm({
      values: store.get("settings"),
      onSave: (v) => actions.saveSettings(v),
      sections: [{ title: t("Webhook security"), hint: t("Dashboard access is handled by your account login. This passphrase, if set, is an optional extra check applied to every webhook's JSON body (\"passphrase\": \"…\")."), fields: [
        { name: "webhook_passphrase", type: "password", label: t("Webhook passphrase (optional)"), placeholder: t("leave empty to disable") },
      ] }],
    });
    root.append(pageHead(t("Security"), lead()), form.el);
    const unsub = store.subscribe("settings", (s) => { if (!form.isDirty()) form.setValues(s); });
    return () => unsub();
  },
};

export const updates = {
  title: t("Updates"),
  gate: "admin",
  render(root) {
    const status = h("div", { class: "callout" }, t("Checking…"));
    const applyBtn = h("button", { type: "button", class: "btn btn-update hidden", onClick: async () => {
      if (!(await confirmDialog({ title: t("Update now?"), body: t("Pull the latest version from GitHub and restart the bridge. Open positions are not affected; the dashboard reloads in a few seconds."), confirmText: t("Update & restart") }))) return;
      if (applyBtn.disabled) return;
      applyBtn.disabled = true;
      toast("Updating…");
      try {
        const r = await api.post("/api/update/apply");
        toast(r.message, "success");
        setTimeout(() => window.location.reload(), 5000);
      } catch (e) { toast("Update failed: " + e.message, "error"); applyBtn.disabled = false; }
    } }, t("Update & restart"));
    const checkBtn = h("button", { type: "button", class: "btn btn-secondary", onClick: () => actions.checkUpdate() }, icon("refresh"), t("Check now"));
    const backupLink = h("a", { class: "btn btn-ghost", href: "/api/update/backup", download: "", title: t("The whole database as one SQLite file — restore it on another server with: fluxbridge restore FILE") }, icon("download"), t("Download backup"));
    const hosting = h("p", { class: "hint", style: "margin-top:10px" }, t("Moving to your own Linux server? One line installs everything (HTTPS, service, daily backups): "),
      h("code", null, t("curl -fsSL https://raw.githubusercontent.com/tobiasgiger/nexuspred/main/deploy/install-server.sh | sudo bash -s -- --domain YOUR.DOMAIN")),
      t(" — see docs/SELF-HOSTING.md. Download the backup here first and restore it there."));
    const form = settingsForm({
      values: store.get("settings"),
      onSave: (v) => actions.saveSettings(v),
      sections: [{ title: t("Self-updater"), fields: [
        { name: "auto_check_updates", type: "switch", label: t("Auto-check for updates") },
      ], after: h("div", null, status, h("div", { class: "form-actions" }, checkBtn, applyBtn, backupLink), hosting) },
      { title: t("Settings file"), hint: t("The workspace configuration as one JSON file: webhooks with routing, symbol map, trading rules, alert preferences, news-lock rules. No secrets travel (broker tokens, passwords, API keys). Use it as a configuration backup or to move a workspace to another bridge — the database backup above is the full copy."), after: settingsFilePanel() }],
    });
    root.append(pageHead(t("Updates"), t("Version status of this bridge and the one-click updater (self-hosted installs).")), form.el);
    const unsubs = [
      store.subscribe("settings", (s) => { if (!form.isDirty()) form.setValues(s); }),
      store.subscribe("update", (u) => {
        if (!u) return;
        if (u.error) { status.className = "callout warn"; status.textContent = "⚠ " + u.error; applyBtn.classList.add("hidden"); return; }
        if (u.update_available) {
          status.className = "callout warn";
          status.textContent = t("New version {latest} available (current {current}) — branch {branch}, {repo}", { latest: u.latest_version, current: u.current_version, branch: u.branch, repo: u.repo });
          applyBtn.classList.remove("hidden");
        } else {
          status.className = "callout ok";
          status.textContent = t("Up to date (v{current}) — branch {branch}", { current: u.current_version, branch: u.branch });
          applyBtn.classList.add("hidden");
        }
      }, { immediate: true }),
    ];
    if (!store.get("update")) actions.checkUpdate();
    return () => unsubs.forEach((u) => u());
  },
};

/* "Accounts" block: which trade accounts may trigger account-level alerts. */
function alertAccountsPanel() {
  const hint = h("span", { class: "save-hint" });
  const list = h("div", { class: "check-list" });
  const allSwitch = h("input", { type: "checkbox", class: "switch", id: "alert-accounts-all" });
  let selected = new Set((store.get("settings") || {}).alert_accounts || []);
  const allMode = () => selected.size === 0;

  function paint() {
    clear(list);
    const accounts = store.get("tradeAccounts") || [];
    allSwitch.checked = allMode();
    if (!accounts.length) { list.append(h("p", { class: "hint" }, t("No trade accounts discovered yet — connect a login under Broker Accounts first."))); return; }
    for (const a of accounts) {
      const id = `alert-acct-${a.spec}`;
      const box = h("input", { type: "checkbox", id, checked: allMode() || selected.has(a.spec), disabled: allMode(), onChange: (e) => {
        if (e.target.checked) selected.add(a.spec); else selected.delete(a.spec);
        hint.textContent = t("Unsaved changes"); hint.className = "save-hint";
      } });
      list.append(h("label", { class: "check-row", for: id }, box,
        h("span", null, h("strong", null, maskAccount(a.spec) || `#${a.id}`), " ", h("span", { class: "muted" }, `${a.token_name} · ${a.environment}${a.enabled ? "" : t(" · disabled")}`))));
    }
  }
  allSwitch.addEventListener("change", () => {
    if (allSwitch.checked) selected = new Set();
    else selected = new Set((store.get("tradeAccounts") || []).map((a) => a.spec));
    hint.textContent = t("Unsaved changes"); hint.className = "save-hint";
    paint();
  });
  const saveBtn = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
    hint.textContent = t("Saving…"); hint.className = "save-hint";
    try {
      await actions.saveSettings({ alert_accounts: allMode() ? [] : [...selected] });
      hint.textContent = allMode() ? t("Saved — alerts for every account.") : t("Saved — alerts for {n} account(s).", { n: selected.size });
      hint.className = "save-hint ok"; toast("Alert accounts saved", "success");
    } catch (e) { hint.textContent = e.message; hint.className = "save-hint err"; toast(e.message, "error"); }
  } }, t("Save accounts"));
  const el = h("div", null,
    h("label", { class: "check-row", for: "alert-accounts-all", style: "margin-bottom:8px" }, allSwitch, h("span", null, h("strong", null, t("All accounts")), " ", h("span", { class: "muted" }, t("untick to pick specific accounts")))),
    list,
    h("div", { class: "form-actions", style: "margin-top:12px" }, saveBtn, hint));
  // The panel lives inside the settings form; its own inputs must not flip the
  // form's "unsaved changes" bar (they are saved by the button above).
  for (const type of ["input", "change"]) el.addEventListener(type, (e) => e.stopPropagation());
  paint();
  const unsubs = [store.subscribe("tradeAccounts", paint),
    store.subscribe("settings", (s) => { if (hint.textContent !== t("Unsaved changes")) { selected = new Set((s || {}).alert_accounts || []); paint(); } })];
  actions.loadTradeAccounts();
  el.cleanup = () => unsubs.forEach((u) => u());
  return el;
}

/* "Push notifications" block: this device's subscription + every registered device. */
/* Last heartbeat outcome, from /api/status (refreshed with the status poll). */
function heartbeatPanel() {
  const el = h("p", { class: "hint", "data-heartbeat": "" });
  const paint = (st) => {
    const hb = st && st.heartbeat;
    if (!hb || !hb.url) { el.textContent = t("No heartbeat configured."); return; }
    if (hb.at == null) { el.textContent = t("Waiting for the first ping…"); return; }
    el.replaceChildren(hb.ok ? tag(t("delivered"), "ok") : tag(t("failed"), "error"), " ",
      t("Last ping {when}", { when: fmtDateTime(hb.at) }), hb.error ? " — " + hb.error : "");
  };
  el.cleanup = store.subscribe("status", paint, { immediate: true });
  return el;
}

/* Settings export (download via fetch → object URL) and import (file picker → confirm → POST). */
function settingsFilePanel() {
  const hint = h("span", { class: "save-hint" });
  const exportBtn = h("button", { type: "button", class: "btn btn-secondary", onClick: async () => {
    exportBtn.disabled = true;
    try {
      const res = await fetch("/api/settings/export", { credentials: "same-origin" });
      if (!res.ok) throw new Error(res.statusText);
      const blob = await res.blob();
      const name = (res.headers.get("Content-Disposition") || "").match(/filename="([^"]+)"/);
      const a = h("a", { href: URL.createObjectURL(blob), download: name ? name[1] : "fluxbridge-settings.json" });
      document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    } catch (e) { toast(t("Export failed: {error}", { error: e.message }), "error"); }
    exportBtn.disabled = false;
  } }, icon("download"), t("Export settings"));
  const file = h("input", { type: "file", accept: "application/json,.json", hidden: true });
  file.addEventListener("change", async () => {
    const f = file.files && file.files[0];
    file.value = "";
    if (!f) return;
    let doc;
    try { doc = JSON.parse(await f.text()); } catch { toast(t("Not a JSON file"), "error"); return; }
    const keys = doc && doc.settings && typeof doc.settings === "object" ? Object.keys(doc.settings) : [];
    if (doc?.fluxbridge_settings !== 1 || !keys.length) { toast(t("Not a Fluxbridge settings export"), "error"); return; }
    const whs = Array.isArray(doc.settings.webhooks) ? doc.settings.webhooks.length : 0;
    const ok = await confirmDialog({ title: t("Import settings?"), danger: true, confirmText: t("Import"),
      body: t("{n} setting(s) from {file} (exported {when}) replace the current values, including {w} webhook(s) — the current webhook list is overwritten. Broker logins and secrets are not touched.",
        { n: keys.length, file: f.name, when: doc.exported_at ? fmtDateTime(doc.exported_at) : "?", w: whs }) });
    if (!ok) return;
    hint.textContent = t("Importing…");
    try {
      const r = await api.post("/api/settings/import", doc);
      toast(t("Settings imported ({n} keys)", { n: r.keys.length }), "success");
      hint.textContent = "";
      await actions.loadSettings();
      actions.refreshStatus();
    } catch (e) { hint.textContent = ""; toast(t("Import failed: {error}", { error: e.message }), "error"); }
  });
  const importBtn = h("button", { type: "button", class: "btn btn-ghost", onClick: () => file.click() }, icon("inbox"), t("Import settings…"));
  return h("div", { class: "form-actions" }, exportBtn, importBtn, file, hint);
}

function pushPanel() {
  const status = h("p", { class: "hint" }, t("Checking this device…"));
  const enableBtn = h("button", { type: "button", class: "btn btn-primary", disabled: true }, icon("bell"), t("Enable on this device"));
  const disableBtn = h("button", { type: "button", class: "btn btn-ghost", hidden: true }, t("Disable on this device"));
  const testAllBtn = h("button", { type: "button", class: "btn btn-secondary", hidden: true }, icon("send"), t("Test push"));
  const table = dataTable({ empty: t("No device registered yet."), compact: true, columns: [
    { label: t("Device"), render: (d) => [h("strong", null, d.device || "Device"), " ", h("span", { class: "muted" }, d.endpoint_host || "")] },
    { label: t("Added"), render: (d) => fmtDateTime(d.created_at) },
    { label: t("Last push"), render: (d) => d.last_used_at ? fmtDateTime(d.last_used_at) : "—" },
    { label: t("Status"), render: (d) => d.failures ? [tag(`failing (${d.failures})`, "error"), d.last_error ? h("div", { class: "muted", style: "font-size:.8em;margin-top:4px;max-width:260px;word-break:break-word" }, d.last_error) : null] : tag("ok", "ok") },
    { label: "", render: (d) => h("div", { class: "inline-actions" },
      h("button", { type: "button", class: "btn btn-ghost btn-sm", title: t("Send a test push to this device"), onClick: async () => {
        try { const r = await api.post("/api/push/test", { id: d.id }); toast(r.sent ? t("Test push sent") : `Not delivered: ${r.gone ? t("device unsubscribed") : t("push service rejected it")}`, r.sent ? "success" : "error"); load(); }
        catch (e) { toast(e.message, "error"); }
      } }, icon("send")),
      h("button", { type: "button", class: "btn btn-ghost btn-sm", title: t("Remove"), onClick: async () => {
        if (!(await confirmDialog({ title: t("Remove \"{device}\"?", { device: d.device || t("this device") }), body: t("The device stops receiving push notifications until it is enabled again."), confirmText: t("Remove"), danger: true }))) return;
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
      status.textContent = t("Notifications are blocked for this site. Allow them in the browser / iOS Settings → Notifications and reload.");
      enableBtn.disabled = true; disableBtn.hidden = true;
      return;
    }
    let sub = null;
    try { sub = await currentSubscription(); } catch (e) { sub = null; }
    const known = !!sub && !!(await isKnown(sub.endpoint));
    if (sub && known) {
      status.textContent = t("This device receives push notifications{app}.", { app: isIOS() && isStandalone() ? t(" (Home Screen app)") : "" });
      status.className = "hint";
      enableBtn.disabled = true; enableBtn.hidden = true; disableBtn.hidden = false;
    } else {
      status.textContent = sub ? t("This device has a browser subscription but is not registered with the bridge — press Enable to register it.")
        : (isIOS() ? t("Ready. Press Enable and allow notifications — iOS asks once.") : t("Ready. Press Enable and allow notifications when the browser asks."));
      enableBtn.disabled = false; enableBtn.hidden = false; disableBtn.hidden = true;
    }
  }
  async function isKnown(endpoint) {
    // The bridge never returns full endpoints; compare host + whether *any* device
    // matches this browser's subscription via a HEAD-style check on subscribe.
    try { const r = await api.post("/api/push/known", { endpoint }); return !!r.known; } catch (e) { return false; }
  }
  enableBtn.addEventListener("click", async () => {
    enableBtn.disabled = true; status.textContent = t("Asking for permission…");
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
  const diagOut = h("pre", { hidden: true, style: "white-space:pre-wrap;word-break:break-word;font-size:.78em;background:rgba(127,127,127,.14);color:inherit;padding:10px 12px;border-radius:8px;margin-top:10px;max-height:50vh;overflow:auto" });
  const diagBtn = h("button", { type: "button", class: "btn btn-ghost", onClick: async () => {
    diagOut.hidden = false; diagOut.textContent = t("Running…");
    try {
      const r = await api.post("/api/push/diag");
      const lines = [`server key ${r.public_key_fp} · crypto ${r.crypto_source} · key decrypts=${r.vapid_key_decrypts} current=${r.vapid_key_current}`,
        `sub ${r.claims_sub}`, "", ...((r.devices || []).length ? r.devices.map((d) => `${d.host}\n  ${d.ok ? t("OK — delivered") : d.error}`) : ["no devices registered"])];
      diagOut.textContent = lines.join("\n");
    } catch (e) { diagOut.textContent = e.message; }
  } }, icon("search"), t("Diagnose"));
  const actionsRow = h("div", { class: "form-actions", style: "margin-top:12px" }, enableBtn, disableBtn, testAllBtn, diagBtn);
  const el = h("div", null, status, actionsRow, diagOut,
    h("h3", { style: "margin:18px 0 6px" }, t("Registered devices")),
    h("p", { class: "hint" }, t("Every device that enabled push for this workspace. On iPhone/iPad open the Home Screen app to enable it; Safari tabs can't receive push.")),
    table.el);
  load(); refreshThisDevice();
  return el;
}

export const alerts = {
  title: t("Alerts"),
  render(root) {
    const testHint = h("span", { class: "save-hint" });
    const testBtn = h("button", { type: "button", class: "btn btn-secondary", onClick: async () => {
      testHint.textContent = t("Sending…"); testHint.className = "save-hint";
      try {
        const r = await api.post("/api/alerts/test");
        const on = Object.entries(r.channels || {}).filter(([, v]) => v).map(([k]) => k);
        if (r.status === "none") { testHint.textContent = t("No channel enabled — turn on Discord, email or push, save, then test."); testHint.className = "save-hint err"; toast("No alert channel is enabled", "error"); }
        else { testHint.textContent = t("Sent to: {channels}. Check that it arrived.", { channels: on.join(", ") }); testHint.className = "save-hint ok"; toast("Test alert sent", "success"); }
      } catch (e) { testHint.textContent = e.message; testHint.className = "save-hint err"; toast(e.message, "error"); }
    } }, icon("bell"), t("Send test alert"));
    const accountsPanel = alertAccountsPanel();
    const form = settingsForm({
      values: store.get("settings"),
      onSave: (v) => actions.saveSettings(v),
      sections: [
        { title: t("Discord channel"), hint: t("A Discord webhook URL from the channel's Integrations settings."), fields: [
          { name: "alert_discord_enabled", type: "switch", label: t("Discord alerts enabled") },
          { name: "alert_discord_webhook_url", type: "password", label: t("Discord webhook URL"), placeholder: "https://discord.com/api/webhooks/…" },
          { name: "alert_discord_mention_everyone", type: "switch", label: t("Tag @everyone") },
        ] },
        { title: t("Email channel"), hint: t("SMTP, e.g. Gmail with an App Password (not your login password)."), fields: [
          { name: "alert_email_enabled", type: "switch", label: t("Email alerts enabled") },
          { name: "alert_email_to", type: "email", label: t("Notify email"), placeholder: t("you@example.com") },
          { name: "alert_smtp_host", type: "text", label: t("SMTP host"), placeholder: "smtp.gmail.com" },
          { name: "alert_smtp_port", type: "number", label: t("SMTP port"), placeholder: "587", width: "160px" },
          { name: "alert_smtp_username", type: "text", label: t("SMTP username"), placeholder: t("you@gmail.com") },
          { name: "alert_smtp_password", type: "password", label: t("SMTP password"), placeholder: t("App Password") },
        ] },
        { title: t("Accounts"), hint: t("Which trade accounts may raise account-level alerts: position opened / closed, signal executed and the daily summary. Connection alerts are per login and always fire. Keep this short when you run many mirrored accounts."), after: accountsPanel },
        { title: t("Push notifications"), hint: t("Notifications on your phone or desktop, even when the dashboard is closed. Works in Chrome/Edge/Firefox and on iPhone/iPad (iOS 16.4+) once the dashboard is added to the Home Screen."), fields: [
          { name: "alert_push_enabled", type: "switch", label: t("Push alerts enabled"), hint: t("Master switch for every registered device") },
        ], after: pushPanel() },
        { title: t("Triggers"), hint: t("Each trigger has its own switch. Position opened / closed come from the broker's own position list (polled every few seconds, see Live P&L refresh) and therefore also catch stop and target fills and manual trades."), fields: [
          { name: "alert_on_connection_lost", type: "switch", label: t("Connection lost"), hint: t("Which account + broker — Discord + email") },
          { name: "alert_on_connection_restored", type: "switch", label: t("Connection restored"), hint: t("Discord + email") },
          { name: "alert_on_trade_executed", type: "switch", label: t("Signal executed"), hint: t("A webhook / Discord signal was sent to the broker: strategy, action, contract, accounts — Discord + push") },
          { name: "alert_on_trade_opened", type: "switch", label: t("Position opened"), hint: t("Seen on the broker side, so manual entries count too: account, symbol, direction, size, price — Discord + push") },
          { name: "alert_on_trade_closed", type: "switch", label: t("Position closed"), hint: t("Including stop / target fills: account, symbol, direction, size, realised P&L, duration — Discord + push. Partial closes are reported as 'reduced'.") },
          { name: "alert_on_agent_lost", type: "switch", label: t("Execution agent went offline"), hint: t("A paired VPS agent stopped polling — Discord + email + push") },
          { name: "alert_on_agent_restored", type: "switch", label: t("Execution agent came back online"), hint: t("Discord + email + push") },
          { name: "alert_on_risk", type: "switch", label: t("Risk guard fired"), hint: t("An account hit its daily loss / profit limit or flatten time and was flattened + locked — all channels") },
          { name: "alert_on_copy", type: "switch", label: t("Copy trading"), hint: t("A follower's mirror order was rejected (Discord + push) or a group paused itself after a feed loss (all channels)") },
          { name: "alert_daily_summary", type: "switch", label: t("Daily summary"), hint: t("Once a day: realised P&L per account, trades closed, wins / losses — all channels") },
          { name: "daily_summary_time", type: "text", label: t("Daily summary time (HH:MM)"), placeholder: "22:05", width: "160px", hint: t("Local time in the journal timezone (Settings → General → Trading journal).") },
          { name: "alert_on_webhook_failed", type: "switch", label: t("Signal received but not executed"), hint: t("Webhook failure — Discord + email") },
          { name: "alert_on_discord_lost", type: "switch", label: t("Discord listener went offline"), hint: t("Discord + email") },
          { name: "alert_on_discord_restored", type: "switch", label: t("Discord listener came back online"), hint: t("Discord + email") },
          { name: "alert_on_rollover", type: "switch", label: t("Contract rollover due"), hint: t("A dated contract in the symbol map is near or past its roll date — Discord + email + push, once per contract") },
          { name: "rollover_warn_days", type: "number", label: t("Rollover warning lead time (days)"), min: 0, max: 60, placeholder: "10", width: "200px", hint: t("Warn this many days before the estimated expiry / first-notice date.") },
          { name: "discord_health_grace", type: "number", label: t("Discord health grace period (seconds)"), min: 15, step: 5, placeholder: "90", width: "200px", hint: t("How long the listener may be down before an outage alert fires (avoids alerting on transient reconnects).") },
        ], after: h("div", { class: "form-actions", style: "margin-top:12px" }, testBtn, testHint) },
        { title: t("External watchdog"), hint: t("The bridge pings a URL you monitor elsewhere (healthchecks.io, Uptime Kuma push monitor, cronitor …). That service alerts you when the pings stop — the one failure the bridge cannot report itself: process gone, host asleep, network down."), fields: [
          { name: "heartbeat_url", type: "text", label: t("Heartbeat URL"), placeholder: "https://hc-ping.com/…", hint: t("Empty = off. Called with a plain GET; anything below HTTP 400 counts as delivered.") },
          { name: "heartbeat_interval", type: "number", label: t("Ping interval (seconds)"), min: 30, max: 3600, step: 10, placeholder: "60", width: "200px", hint: t("30–3600 s. Set the monitor's grace period to about twice this.") },
        ], after: heartbeatPanel() },
      ],
    });
    root.append(pageHead(t("Alerts"), t("Notify a Discord channel, an email address and/or your phone when something happens. ") + lead()), form.el);
    const unsub = store.subscribe("settings", (s) => { if (!form.isDirty()) form.setValues(s); });
    return () => { unsub(); if (accountsPanel.cleanup) accountsPanel.cleanup(); root.querySelectorAll("[data-heartbeat]").forEach((p) => p.cleanup && p.cleanup()); };
  },
};

export const symbols = {
  title: t("Symbol Mapping"),
  render(root) {
    const tbody = h("tbody");
    const row = (tv = "", contract = "") => {
      const tr = h("tr", null,
        h("td", null, h("input", { class: "sm-tv input-sm", value: tv, placeholder: t("MNQ1!") })),
        h("td", null, h("input", { class: "sm-contract input-sm", value: contract, placeholder: "MNQU6" })),
        h("td", { style: "width:44px" }, h("button", { type: "button", class: "btn btn-ghost btn-icon", title: t("Remove"), onClick: () => tr.remove() }, icon("trash"))));
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
      try { await actions.saveSettings({ symbol_map: collect() }); toast("Symbol mapping saved", "success"); loadRollover(true); }
      catch (e) { toast(e.message, "error"); }
    } }, icon("check"), t("Save mapping"));

    // --- Rollover: proposals the user confirms
    const rollBody = h("tbody");
    const rollCard = card({ title: t("Rollover due"), hint: t("Dated contracts near or past their roll date. The next contract is proposed from the broker's listing when a login is connected (otherwise estimated from exchange conventions) — edit it if you prefer another month, tick the rows to roll, then confirm. Nothing changes until you confirm.") },
      h("div", { class: "table-scroll" }, h("table", { class: "data-table compact" }, h("thead", null, h("tr", null, h("th", null, t("Roll")), h("th", null, t("TradingView symbol")), h("th", null, t("Current")), h("th", null, t("Roll date")), h("th", null, t("New contract")), h("th", null, t("Source")))), rollBody)),
      h("div", { class: "form-actions", style: "margin-top:12px" },
        h("button", { type: "button", class: "btn btn-primary", onClick: () => applyRollover() }, icon("check"), t("Apply selected rollovers")),
        h("button", { type: "button", class: "btn btn-ghost", onClick: () => loadRollover(true) }, icon("refresh"), t("Re-check now"))));
    rollCard.hidden = true;
    let rollItems = [];
    function paintRollover(items) {
      rollItems = items || [];
      rollCard.hidden = !rollItems.length;
      rollBody.replaceChildren(...rollItems.map((w) => h("tr", { class: w.stage === "expired" ? "neg" : null },
        h("td", null, h("input", { type: "checkbox", class: "ro-on", checked: true, dataset: { tv: w.tv_symbol } })),
        h("td", null, h("code", null, w.tv_symbol)),
        h("td", null, h("code", null, w.contract)),
        h("td", null, `${w.date_kind} ${w.date} · `, h("span", { class: w.days_left < 0 ? "neg" : "warn" }, w.days_left < 0 ? `${-w.days_left}d ago` : w.days_left === 0 ? "today" : `in ${w.days_left}d`)),
        h("td", null, h("input", { class: "ro-next input-sm", value: w.next || "", style: "width:110px", dataset: { tv: w.tv_symbol } })),
        h("td", null, w.next_source === "broker" ? tag("broker listing", "on") : tag("estimated", "warn"), w.next_expiry ? h("small", { class: "muted", style: "display:block" }, `expires ${w.next_expiry}`) : null))));
    }
    async function loadRollover(refresh = false) {
      try { const r = await api.get(`/api/rollover${refresh ? "?refresh=1" : ""}`); paintRollover(r.rollover); }
      catch (e) { /* the banner on the Overview still shows */ }
    }
    async function applyRollover() {
      const items = [...rollBody.querySelectorAll(".ro-on:checked")].map((cb) => ({ tv_symbol: cb.dataset.tv, contract: (rollBody.querySelector(`.ro-next[data-tv="${CSS.escape(cb.dataset.tv)}"]`).value || "").trim().toUpperCase() })).filter((it) => it.contract);
      if (!items.length) return toast("Nothing selected", "warn");
      const ok = await confirmDialog({ title: t("Apply the rollover?"), body: h("div", null, t("The symbol map changes as follows; new signals trade the new contracts immediately. Open positions and working orders on the old contracts are not touched."),
        h("ul", { style: "margin:8px 0 0 18px" }, items.map((it) => { const w = rollItems.find((x) => x.tv_symbol === it.tv_symbol) || {}; return h("li", null, h("code", null, it.tv_symbol), ": ", h("code", null, w.contract || "?"), " → ", h("code", null, it.contract)); }))), confirmText: t("Apply rollover") });
      if (!ok) return;
      try {
        const r = await api.post("/api/rollover/apply", { items });
        toast(r.changes.length ? t("Rolled {n} symbol(s)", { n: r.changes.length }) : t("Nothing changed"), "success");
        await actions.loadSettings();
        paint(r.symbol_map);
        paintRollover(r.rollover);
        actions.refreshStatus();
      } catch (e) { toast(e.message, "error"); }
    }

    root.append(
      pageHead(t("Symbol Mapping"), t("Maps each TradingView symbol to the exact broker contract used for orders (Tradovate form, e.g. MNQU6; Rithmic and ProjectX logins translate it). When a contract nears its roll date the bridge proposes the next one here — you confirm."), [
        h("button", { type: "button", class: "btn", onClick: () => tbody.append(row()) }, icon("plus"), t("Add row")),
      ]),
      rollCard,
      card({ title: t("Current mapping"), hint: t("Use a dated contract in the Tradovate form (e.g. MNQU6); a bare root (e.g. MNQ) also works and auto-picks the front month. Unmapped symbols are only accepted when their root is in Allowed symbols (General & Trading).") },
        h("div", { class: "table-scroll" }, h("table", { class: "data-table" }, h("thead", null, h("tr", null, h("th", null, t("TradingView symbol")), h("th", null, t("Broker contract")), h("th"))), tbody)),
        h("div", { class: "form-actions", style: "margin-top:12px" }, saveBtn)),
    );
    paint((store.get("settings") || {}).symbol_map);
    loadRollover(true);
    return () => {};
  },
};

export const account = {
  title: t("Account"),
  render(root) {
    const me = store.get("me") || {};
    const cur = h("input", { type: "password", autocomplete: "current-password", required: true });
    const nw = h("input", { type: "password", autocomplete: "new-password", required: true, minlength: 8 });
    const nw2 = h("input", { type: "password", autocomplete: "new-password", required: true, minlength: 8 });
    const hint = h("span", { class: "save-hint" });
    const form = h("form", { autocomplete: "off", onSubmit: async (e) => {
      e.preventDefault();
      if (nw.value !== nw2.value) { hint.textContent = t("New passwords don't match."); hint.className = "save-hint err"; return; }
      if (nw.value.length < 8) { hint.textContent = t("New password must be at least 8 characters."); hint.className = "save-hint err"; return; }
      hint.textContent = t("Saving…"); hint.className = "save-hint";
      try {
        await api.post("/api/account/password", { current: cur.value, new: nw.value });
        form.reset(); hint.textContent = t("Password changed."); hint.className = "save-hint ok"; toast("Password changed", "success");
      } catch (err) { hint.textContent = err.message; hint.className = "save-hint err"; toast(err.message, "error"); }
    } },
      h("div", { class: "grid grid-2" },
        h("div", { class: "field" }, h("label", null, t("Current password")), cur),
        h("div"),
        h("div", { class: "field" }, h("label", null, t("New password (min 8 characters)")), nw),
        h("div", { class: "field" }, h("label", null, t("Confirm new password")), nw2)),
      h("div", { class: "form-actions" }, h("button", { type: "submit", class: "btn btn-primary" }, t("Change password")), hint));
    // ---- two-factor authentication
    const mfaBody = h("div", null, t("Loading…"));
    const codesList = (codes) => h("div", null,
      h("p", { class: "hint", style: "color:var(--yellow)" }, t("Each code signs you in once when your phone is not at hand. They are shown only now — store them in your password manager or print them. You can request a new set under Account at any time; it replaces this one.")),
      h("ul", { class: "mfa-codes" }, codes.map((c) => h("li", null, c))),
      h("div", { class: "form-actions" },
        h("button", { type: "button", class: "btn", onClick: async () => toast((await copyText(codes.join("\n"))) ? t("Copied") : t("Copy failed"), "success") }, icon("copy"), t("Copy codes")),
        h("button", { type: "button", class: "btn btn-primary", onClick: () => { closeDrawer(); paintMfa(); } }, t("I have saved them — continue"))));
    async function paintMfa() {
      clear(mfaBody);
      let st;
      try { st = await api.get("/api/account/2fa"); } catch (err) { mfaBody.append(h("span", { class: "muted" }, err.message)); return; }
      const status = st.enabled ? tag(t("On"), "on") : tag(t("Off"), "off");
      const codes = st.enabled ? h("span", { class: st.backup_codes_left <= 2 ? "neg" : "muted", style: "margin-left:8px" }, t("{n} of {total} backup codes left", { n: st.backup_codes_left, total: st.backup_codes_total })) : null;
      mfaBody.append(h("p", { class: "hint" }, t("A code from your authenticator app is asked for at every sign-in, in addition to the password. Ten single-use backup codes cover a lost phone.")),
        h("div", { style: "display:flex;align-items:center;gap:6px;margin:8px 0 12px" }, status, codes, st.required ? h("span", { class: "muted", style: "margin-left:8px" }, t("required for this account")) : null));
      const actions = h("div", { class: "form-actions" });
      if (!st.enabled) {
        actions.append(h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
          try {
            const r = await api.post("/api/account/2fa/begin");
            const codeInp = h("input", { class: "mfa-code", inputmode: "numeric", autocomplete: "one-time-code", maxlength: 7, placeholder: "123456", style: "text-align:center;font-size:22px;letter-spacing:6px" });
            const err = h("span", { class: "save-hint err" });
            const body = h("div", null,
              h("ol", { class: "mfa-steps" }, h("li", null, t("Install an authenticator app (Google Authenticator, Microsoft Authenticator, Authy, 1Password, Aegis …).")), h("li", null, t("Scan this QR code, or type the key by hand.")), h("li", null, t("Enter the 6-digit code the app shows."))),
              h("img", { class: "mfa-qr", src: r.qr, alt: "QR" }), h("code", { class: "mfa-secret" }, r.secret),
              h("div", { class: "field" }, h("label", null, t("6-digit code")), codeInp), err);
            const confirmBtn = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
              try {
                const res = await api.post("/api/account/2fa/confirm", { code: codeInp.value });
                toast(t("Two-factor authentication enabled"), "success");
                openDrawer({ title: t("Save these backup codes now"), body: codesList(res.backup_codes), width: "520px", onClose: paintMfa });
              } catch (e) { err.textContent = e.message; }
            } }, t("Activate"));
            openDrawer({ title: t("Set up two-factor authentication"), body, width: "520px", foot: h("div", { class: "form-actions" }, h("button", { type: "button", class: "btn btn-ghost", onClick: () => closeDrawer() }, t("Cancel")), confirmBtn) });
            codeInp.focus();
          } catch (e) { toast(e.message, "error"); }
        } }, icon("shield"), t("Enable two-factor authentication")));
      } else {
        const askPwCode = (title, onSubmit) => {
          const pw = h("input", { type: "password", autocomplete: "current-password" });
          const code = h("input", { class: "mfa-code", inputmode: "numeric", autocomplete: "one-time-code", maxlength: 7, style: "text-align:center;letter-spacing:4px" });
          const err = h("span", { class: "save-hint err" });
          const go = h("button", { type: "button", class: "btn btn-primary", onClick: async () => { try { await onSubmit(pw.value, code.value); } catch (e) { err.textContent = e.message; } } }, t("Continue"));
          openDrawer({ title, width: "440px", body: h("div", null,
            h("div", { class: "field" }, h("label", null, t("Current password")), pw),
            h("div", { class: "field" }, h("label", null, t("Code from your authenticator app")), code), err),
            foot: h("div", { class: "form-actions" }, h("button", { type: "button", class: "btn btn-ghost", onClick: () => closeDrawer() }, t("Cancel")), go) });
          pw.focus();
        };
        actions.append(h("button", { type: "button", class: "btn", title: t("Replaces the current set — lost or used-up codes stop working."), onClick: () => askPwCode(t("New backup codes"), async (password, code) => {
          const res = await api.post("/api/account/2fa/backup-codes", { password, code });
          toast(t("New backup codes issued"), "success");
          openDrawer({ title: t("Save these backup codes now"), body: codesList(res.backup_codes), width: "520px", onClose: paintMfa });
        }) }, icon("refresh"), t("New backup codes")));
        if (!st.required) actions.append(h("button", { type: "button", class: "btn btn-ghost", onClick: () => askPwCode(t("Disable two-factor authentication?"), async (password, code) => {
          await api.post("/api/account/2fa/disable", { password, code }); closeDrawer(); toast(t("Two-factor authentication disabled"), "warn"); paintMfa();
        }) }, t("Disable")));
      }
      mfaBody.append(actions);
    }
    paintMfa();
    root.append(
      pageHead(t("Account"), t("You're signed in to your own isolated area — token accounts, webhooks, Discord listener, symbol map and logs are private to you.")),
      h("div", { class: "grid grid-2" },
        card({ title: t("Two-factor authentication") }, mfaBody),
        card({ title: t("Your account") },
          h("dl", { class: "kv" }, h("dt", null, t("Email")), h("dd", null, me.email || "—"), h("dt", null, t("Role")), h("dd", null, me.is_admin ? t("Administrator") : t("User")),
            h("dt", null, t("Discord Signals")), h("dd", null, (me.features || {}).discord_signals === false ? t("not enabled") : "enabled")),
          h("div", { class: "form-actions", style: "margin-top:14px" },
            h("button", { type: "button", class: "btn btn-ghost", onClick: async () => {
              try { await fetch("/logout", { method: "POST", credentials: "same-origin", redirect: "manual" }); } catch { /* cookie cleared server-side */ }
              window.location.href = "/login";
            } }, icon("logout"), t("Sign out")),
            h("button", { type: "button", class: "btn btn-ghost", title: t("Every other browser and phone signed in to this account is logged out; this one stays."), onClick: async () => {
              if (!(await confirmDialog({ title: t("Sign out other devices?"), body: t("Every other browser or phone signed in to your account is logged out immediately. This device stays signed in."), confirmText: t("Sign out others") }))) return;
              try { await api.post("/api/account/sessions/revoke"); toast("Other devices signed out", "success"); } catch (err) { toast(err.message, "error"); }
            } }, t("Sign out other devices")))),
        card({ title: t("Change password") }, form)),
    );
    return () => {};
  },
};
