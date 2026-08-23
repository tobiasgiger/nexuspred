"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

function toast(msg, type = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show " + type;
  setTimeout(() => (t.className = "toast " + type), 3200);
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/* ------------------------------------------------------- sidebar nav */
const appShell = $("#appShell");
const settingsChildren = $("#settingsChildren");
const settingsParent = document.querySelector('.nav-parent[data-parent="settings"]');

function activateTab(name) {
  const panel = $("#tab-" + name);
  if (!panel) return;
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".panel").forEach((p) => p.classList.remove("active"));
  panel.classList.add("active");
  // Parent "Settings" is highlighted + expanded whenever a sub-page is shown.
  const isSettings = name.startsWith("settings-");
  if (settingsParent) settingsParent.classList.toggle("active", isSettings);
  if (isSettings && settingsChildren) {
    settingsChildren.classList.add("open");
    settingsParent.classList.add("expanded");
  }
  if (appShell) appShell.classList.remove("sidebar-open");   // close mobile drawer
}

$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => activateTab(tab.dataset.tab));
});

// "Settings" group: expand/collapse; opening jumps to the first sub-page.
if (settingsParent && settingsChildren) {
  settingsParent.addEventListener("click", () => {
    if (appShell && appShell.classList.contains("collapsed")) setSidebarCollapsed(false);
    const nowOpen = settingsChildren.classList.toggle("open");
    settingsParent.classList.toggle("expanded", nowOpen);
    const active = document.querySelector(".panel.active");
    if (nowOpen && (!active || !active.id.startsWith("tab-settings-"))) {
      activateTab("settings-general");
    }
  });
}

// Collapse the sidebar to an icon rail (persisted per browser).
function setSidebarCollapsed(on) {
  if (appShell) appShell.classList.toggle("collapsed", on);
  try { localStorage.setItem("np_sidebar_collapsed", on ? "1" : "0"); } catch (e) { /* ignore */ }
}
const sidebarCollapseBtn = $("#sidebarCollapse");
if (sidebarCollapseBtn) {
  sidebarCollapseBtn.addEventListener("click", () =>
    setSidebarCollapsed(!appShell.classList.contains("collapsed")));
}
try { if (localStorage.getItem("np_sidebar_collapsed") === "1") setSidebarCollapsed(true); } catch (e) { /* ignore */ }

// Mobile off-canvas drawer.
const hamburgerBtn = $("#hamburger");
if (hamburgerBtn) hamburgerBtn.addEventListener("click", () => appShell.classList.toggle("sidebar-open"));
const scrimEl = $("#scrim");
if (scrimEl) scrimEl.addEventListener("click", () => appShell.classList.remove("sidebar-open"));

/* ----------------------------------------------------- collapsible cards */
// Only the Webhooks, Settings and Setup Guide tabs get the accordion
// treatment (they're the ones with many stacked cards) — Monitor, Logs,
// Test & Webhook and Simulator stay as always-open cards. Within a treated
// tab, every .card collapses by default except small stat tiles, the guide's
// section dividers, and its intro card (holds the TOC). Safe to call
// repeatedly (e.g. after re-rendering the Webhooks list) — already-wired
// cards are skipped via the collapsibleInit marker.
function makeCardsCollapsible(root = document) {
  root.querySelectorAll(".card").forEach((card) => {
    if (card.dataset.collapsibleInit) return;
    if (card.classList.contains("stat") || card.classList.contains("part-divider")
      || card.classList.contains("guide-intro")) return;
    const head = card.querySelector(":scope > .card-head");
    if (!head) return;
    card.dataset.collapsibleInit = "1";
    card.classList.add("collapsible", "collapsed");

    const body = document.createElement("div");
    body.className = "card-body";
    while (head.nextSibling) body.appendChild(head.nextSibling);
    card.appendChild(body);

    // Group any existing header controls (e.g. "+ Add", the webhook Enabled
    // switch) together with the toggle chevron so they sit flush at the
    // right edge, instead of floating in the middle of the header.
    const actions = document.createElement("div");
    actions.className = "card-head-actions";
    while (head.children.length > 1) actions.appendChild(head.children[1]);
    head.appendChild(actions);

    const toggle = document.createElement("span");
    toggle.className = "card-toggle";
    toggle.textContent = "▸";
    actions.appendChild(toggle);

    head.addEventListener("click", (e) => {
      if (e.target.closest("button, a, input, select, textarea, label")) return;
      const wasCollapsed = card.classList.contains("collapsed");
      // Expanding a card next to a still-collapsed sibling in the same grid
      // row left the sibling stretched to match height but empty-looking
      // (CSS grid rows share a height). Expand row-mates together instead.
      let rowMates = [];
      if (wasCollapsed) {
        const grid = card.parentElement;
        if (grid && grid.classList.contains("grid")) {
          const myTop = card.getBoundingClientRect().top;
          rowMates = [...grid.children].filter((c) =>
            c !== card && c.classList.contains("collapsible") &&
            Math.abs(c.getBoundingClientRect().top - myTop) < 2);
        }
      }
      card.classList.toggle("collapsed");
      rowMates.forEach((c) => c.classList.remove("collapsed"));
    });
  });
}

// Jumping to an anchor (e.g. the guide's table of contents) should expand
// whatever collapsed card it lands in, not just scroll to a closed card.
$$('a[href^="#"]').forEach((a) => {
  a.addEventListener("click", () => {
    const target = document.getElementById(a.getAttribute("href").slice(1));
    if (target) target.classList.remove("collapsed");
  });
});

["#tab-guide"].forEach((sel) => makeCardsCollapsible($(sel)));

// "Configure →" shortcut (e.g. from the Discord tab) that switches to a tab.
$$("[data-jump]").forEach((el) => {
  el.addEventListener("click", (e) => {
    e.preventDefault();
    const btn = document.querySelector(`.tab[data-tab="${el.dataset.jump}"]`);
    if (btn) btn.click();
  });
});

/* --------------------------------------------------------------- status */
async function refreshStatus() {
  try {
    const s = await api("/api/status");
    $("#versionText").textContent = "v" + s.version;

    const conn = s.connection || {};
    $("#connDot").className = "dot" + (conn.connected ? " on" : "");
    const total = conn.accounts_total || 0;
    const con = conn.accounts_connected || 0;
    $("#connText").textContent = total ? `${con}/${total} connected` : "Disconnected";

    const trading = s.trading_enabled;
    const te = $("#statTrading");
    te.textContent = trading ? "ENABLED" : "DISABLED";
    te.className = "status-v " + (trading ? "on" : "off");
    $("#statEnv").textContent = total ? `${con}/${total} login${total === 1 ? "" : "s"}` : "—";

    const ta = s.trade_accounts || [];
    const taConn = ta.filter((a) => a.connected).length;
    $("#statAccount").textContent = ta.length ? `${taConn}/${ta.length} connected` : "—";

    renderSessions(s.sessions || []);
    renderActive(s.active_trades || {});
  } catch (e) { /* status polling is best-effort */ }
}

function renderSessions(sessions) {
  const tbody = $("#sessionsTable tbody");
  if (!sessions.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty">No accounts configured</td></tr>';
    return;
  }
  tbody.innerHTML = sessions.map((x) => {
    const ok = !!x.connected;
    return `<tr><td>${escapeHtml(x.name || "—")}</td>
      <td>${(x.environment || "—").toUpperCase()}</td>
      <td class="${ok ? "pos" : "neg"}">${ok ? "Connected" : "Disconnected"}</td>
      <td>${fmtDateTime(x.token_expires)}</td>
      <td>${fmtDateTime(x.last_renew)}</td>
      <td class="${x.last_error ? "neg" : ""}">${escapeHtml(x.last_error || "—")}</td></tr>`;
  }).join("");
}

function fmtDateTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString([], { month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function renderActive(trades) {
  const tbody = $("#activeTable tbody");
  const rows = [];
  for (const [key, t] of Object.entries(trades)) {
    const accts = t.accounts || {};
    const ids = Object.keys(accts);
    if (!ids.length) continue;
    const sym = t.root || key.split(":").pop();
    const whName = t.webhook_name || "—";
    for (const id of ids) {
      const a = accts[id];
      rows.push(`<tr><td>${escapeHtml(whName)}</td><td>${sym}</td><td>${t.contract}</td>
        <td><span class="tag ${t.side}">${(t.side || "").toUpperCase()}</span></td>
        <td>${a.name || id}</td><td>${a.qty ?? "—"}</td>
        <td>${a.sl_order_id || "—"}</td>
        <td>${(a.tp_order_ids || []).join(", ") || "—"}</td></tr>`);
    }
  }
  tbody.innerHTML = rows.length ? rows.join("")
    : '<tr><td colspan="8" class="empty">None</td></tr>';
}

/* --------------------------------------------------------------- orders */
async function refreshOrders() {
  try {
    const orders = await api("/api/orders");
    const tbody = $("#ordersTable tbody");
    if (!orders.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="empty">No orders yet</td></tr>';
      return;
    }
    tbody.innerHTML = orders.map((o) => {
      const price = o.price ?? o.stop_price ?? "—";
      const side = (o.action || "").toLowerCase();
      const sideClass = side === "buy" ? "buy" : side === "sell" ? "sell" : "";
      const statusClass = (o.status || "").includes("reject") ? "rejected" : "ok";
      const sim = o.simulated ? ' <span class="tag sim">SIM</span>' : "";
      return `<tr><td>${fmtTime(o.ts)}</td>
        <td><span class="tag ${sideClass}">${o.action}</span></td>
        <td>${o.symbol}${sim}</td><td>${o.qty}</td><td>${o.order_type}</td>
        <td>${price}</td><td><span class="tag ${statusClass}">${o.status}</span></td></tr>`;
    }).join("");
  } catch (e) { /* ignore */ }
}

/* --------------------------------------------------------------- positions */
async function refreshPositions() {
  const tbody = $("#positionsTable tbody");
  try {
    const positions = await api("/api/positions");
    if (!positions.length) {
      tbody.innerHTML = '<tr><td colspan="3" class="empty">No open positions</td></tr>';
      return;
    }
    tbody.innerHTML = positions.map((p) => {
      const net = p.netPos ?? 0;
      const netClass = net >= 0 ? "pos" : "neg";
      return `<tr><td>${p.symbol ?? "—"}</td>
        <td class="${netClass}">${net}</td><td>${p.netPrice ?? "—"}</td></tr>`;
    }).join("");
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="3" class="empty">${e.message}</td></tr>`;
  }
}

/* --------------------------------------------------------------- logs */
async function refreshLogs() {
  try {
    const [events, signals] = await Promise.all([
      api("/api/events"), api("/api/signals"),
    ]);
    $("#eventLog").innerHTML = events.map((e) =>
      `<div class="log-line"><span class="lt">${fmtTime(e.ts)}</span>
       <span class="lv ${e.level}">${e.level.toUpperCase()}</span>
       <span>${escapeHtml(e.message)}</span></div>`).join("") ||
      '<div class="empty">No events</div>';

    $("#signalLog").innerHTML = signals.map((s) =>
      `<div class="log-line"><span class="lt">${fmtTime(s.ts)}</span>
       <span class="lv info">${escapeHtml(s.result || "")}</span>
       <code>${escapeHtml(JSON.stringify(s.payload))}</code></div>`).join("") ||
      '<div class="empty">No signals</div>';
  } catch (e) { /* ignore */ }
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

/* --------------------------------------------------------------- settings */
async function loadSettings() {
  const s = await api("/api/settings");
  const form = $("#settingsForm");
  for (const [key, val] of Object.entries(s)) {
    const el = form.elements[key];
    if (!el) continue;
    if (el.type === "checkbox") el.checked = !!val;
    else if (key === "symbol_map") el.value = JSON.stringify(val, null, 2);
    else if (key === "allowed_symbols" || key === "google_allowed_emails") el.value = (val || []).join(", ");
    else el.value = val ?? "";
  }
  renderSymbolMap(s.symbol_map || {});
}

$("#settingsForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const payload = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    if (el.type === "checkbox") payload[el.name] = el.checked;
    else if (el.type === "number") payload[el.name] = Number(el.value);
    else if (el.name === "symbol_map") {
      try { payload[el.name] = JSON.parse(el.value || "{}"); }
      catch { return toast("Symbol map must be valid JSON", "error"); }
    } else if (el.name === "allowed_symbols" || el.name === "google_allowed_emails") {
      payload[el.name] = el.value.split(",").map((x) => x.trim()).filter(Boolean);
    } else payload[el.name] = el.value;
  }
  try {
    await api("/api/settings", { method: "POST", body: JSON.stringify(payload) });
    toast("Settings saved", "success");
    $("#saveHint").textContent = "Saved ✓";
    setTimeout(() => ($("#saveHint").textContent = ""), 2500);
    refreshStatus();
  } catch (err) { toast(err.message, "error"); }
});

async function connectAll() {
  toast("Connecting…");
  try {
    const r = await api("/api/connect", { method: "POST" });
    const ok = (r.sessions || []).filter((x) => x.connected).length;
    toast(`Connected ${ok}/${(r.sessions || []).length} account(s)`,
      ok ? "success" : "error");
    renderSessions(r.sessions || []);
    await loadTradeAccounts();   // connect discovers the trade accounts under each login
    renderWebhooks();            // refresh webhook cards with any newly discovered accounts
    refreshStatus();
  } catch (e) { toast("Connect failed: " + e.message, "error"); }
}
$("#connectBtn").addEventListener("click", connectAll);
$("#connectTokenBtn").addEventListener("click", connectAll);

/* --------------------------------------------------------------- updates */
async function checkUpdate() {
  const box = $("#updateStatus");
  try {
    const u = await api("/api/update/check");
    if (u.error) {
      box.textContent = "⚠ " + u.error;
      box.className = "update-status";
      return;
    }
    if (u.update_available) {
      box.textContent = `New version ${u.latest_version} available (current ${u.current_version})`;
      box.className = "update-status available";
      $("#updateBtn").classList.remove("hidden");
    } else {
      box.textContent = `Up to date (v${u.current_version})`;
      box.className = "update-status current";
      $("#updateBtn").classList.add("hidden");
    }
  } catch (e) {
    box.textContent = "⚠ " + e.message;
    box.className = "update-status";
  }
}

async function applyUpdate() {
  if (!confirm("Pull the latest version from GitHub and restart the bridge?")) return;
  toast("Updating…");
  try {
    const r = await api("/api/update/apply", { method: "POST" });
    toast(r.message, "success");
    setTimeout(() => location.reload(), 5000);
  } catch (e) { toast("Update failed: " + e.message, "error"); }
}

$("#updateBtn").addEventListener("click", applyUpdate);
$("#checkUpdateBtn").addEventListener("click", checkUpdate);

/* --------------------------------------------------------------- webhook/test */
function updateWebhookUrl(token) {
  const t = token || "your-token";
  const url = `${location.origin}/webhook/${t}`;
  $("#webhookUrl").textContent = url;
  const guideUrl = $("#guideUrl");
  if (guideUrl) guideUrl.textContent = url;
}

$("#copyUrl").addEventListener("click", () => {
  navigator.clipboard.writeText($("#webhookUrl").textContent);
  toast("Webhook URL copied", "success");
});

/* ------------------------------------------------------- account / users */
async function loadAccount() {
  let me;
  try { me = await api("/api/me"); } catch (e) { return; }
  const email = me.email || "";
  const ue = $("#userEmail"); if (ue) ue.textContent = email;
  const ae = $("#accEmail"); if (ae) ae.textContent = email;
  const ar = $("#accRole"); if (ar) ar.textContent = me.is_admin ? "Admin" : "User";
  const adminCard = $("#usersAdminCard");
  if (adminCard) {
    adminCard.classList.toggle("hidden", !me.is_admin);
    if (me.is_admin) { loadUsers(); loadInvites(); }
  }
}

async function loadUsers() {
  try {
    const users = await api("/api/users");
    const me = await api("/api/me");
    $("#usersTable tbody").innerHTML = users.map((u) => `
      <tr>
        <td>${escapeHtml(u.email)}</td>
        <td>${u.is_admin ? '<span class="tag">admin</span>' : "user"}</td>
        <td>${fmtDateTime(u.created_at)}</td>
        <td>${u.id === me.id ? '<span class="hint">you</span>'
          : `<button type="button" class="btn btn-ghost user-del" data-id="${u.id}" data-email="${escapeHtml(u.email)}">Delete</button>`}</td>
      </tr>`).join("");
    $("#usersTable tbody").querySelectorAll(".user-del").forEach((b) =>
      b.addEventListener("click", async () => {
        if (!confirm(`Delete ${b.dataset.email}? Their area and all its data are removed. This cannot be undone.`)) return;
        try { await api(`/api/users/${b.dataset.id}`, { method: "DELETE" }); toast("User deleted", "success"); loadUsers(); }
        catch (e) { toast(e.message, "error"); }
      }));
  } catch (e) { /* ignore */ }
}

async function loadInvites() {
  try {
    const invites = await api("/api/invites");
    const open = invites.filter((i) => !i.used_by);
    const body = $("#invitesTable tbody");
    body.innerHTML = open.length ? open.map((i) => {
      const url = `${location.origin}/register?code=${i.code}`;
      return `<tr>
        <td><code style="font-size:11px">${escapeHtml(url)}</code></td>
        <td>${escapeHtml(i.email || "anyone")}</td>
        <td>${i.is_admin ? "yes" : "no"}</td>
        <td>open</td>
        <td><button type="button" class="btn btn-ghost inv-del" data-code="${i.code}">Revoke</button></td>
      </tr>`;
    }).join("") : '<tr><td colspan="5" class="empty">None</td></tr>';
    body.querySelectorAll(".inv-del").forEach((b) =>
      b.addEventListener("click", async () => {
        try { await api(`/api/invites/${b.dataset.code}`, { method: "DELETE" }); loadInvites(); }
        catch (e) { toast(e.message, "error"); }
      }));
  } catch (e) { /* ignore */ }
}

const _createInviteBtn = $("#createInviteBtn");
if (_createInviteBtn) _createInviteBtn.addEventListener("click", async () => {
  try {
    const r = await api("/api/users/invite", {
      method: "POST",
      body: JSON.stringify({ is_admin: $("#inviteIsAdmin").checked }),
    });
    $("#inviteUrl").textContent = r.url;
    $("#inviteResult").classList.remove("hidden");
    try { await navigator.clipboard.writeText(r.url); } catch (e) { /* ignore */ }
    toast("Invite link created & copied", "success");
    loadInvites();
  } catch (e) { toast(e.message, "error"); }
});

const _copyInvite = $("#copyInvite");
if (_copyInvite) _copyInvite.addEventListener("click", () => {
  navigator.clipboard.writeText($("#inviteUrl").textContent);
  toast("Invite link copied", "success");
});

/**
 * TradingView alert-message JSON for a given strategy type, using TradingView's
 * own placeholders ({{strategy.order.action}}, {{strategy.order.contracts}},
 * {{ticker}}, {{strategy.order.price}}) so it can be pasted straight into the
 * alert's Message box. Numeric fields are unquoted so the substituted value
 * stays a JSON number, not a string.
 */
function alertMessageTemplate(strategy) {
  if (strategy === "ts_hunter") {
    const json = JSON.stringify({
      contract_version: "at_execution_command_v5", event: "signal",
      side: "BUY", symbol: "MNQ",
      risk: { mode: "fixed_lot", value: 4 },
      sl: { mode: "fixed_price_from_alert", value: 0 },
      trade_id: "unique-id-per-trade",
    }, null, 2);
    return {
      json,
      hint: "TS-Hunter expects the exact JSON your TS-Hunter Pine strategy already sends "
        + "(entry as event:\"signal\", then event:\"management\" messages with "
        + "action:\"partial_close_percent\" for TP1/TP2/TP3 and action:\"full_close\" to "
        + "flatten) — all correlated by trade_id. Point that strategy's alert(s) at this "
        + "webhook's URL; there's nothing to hand-edit here. Each partial close resizes "
        + "the stop to the new remaining qty (price unchanged).",
    };
  }
  if (strategy === "bracket") {
    const json = JSON.stringify({
      action: "{{strategy.order.action}}",
      symbol: "{{ticker}}",
      entry: "{{strategy.order.price}}",
      sl: 0, tp1: 0, tp2: 0, tp3: 0,
    }, null, 2).replace('"{{strategy.order.price}}"', "{{strategy.order.price}}");
    return {
      json,
      hint: "action/symbol/entry are filled in automatically by TradingView. There's no "
        + "built-in placeholder for sl/tp1/tp2/tp3 — replace those 0s with your own "
        + "strategy's stop/target levels (e.g. {{plot(\"SL\")}} if you plot them in Pine), "
        + "or drop any tp you don't use.",
    };
  }
  const json = JSON.stringify({
    action: "{{strategy.order.action}}",
    symbol: "{{ticker}}",
    qty: "{{strategy.order.contracts}}",
  }, null, 2).replace('"{{strategy.order.contracts}}"', "{{strategy.order.contracts}}");
  return {
    json,
    hint: "action, symbol and qty are filled in automatically by TradingView from the "
      + "strategy order — nothing to edit.",
  };
}

function renderAlertTemplate(webhook, preEl, hintEl) {
  if (!webhook) {
    preEl.textContent = "—";
    hintEl.textContent = "";
    return;
  }
  const t = alertMessageTemplate(webhook.strategy);
  preEl.textContent = t.json;
  hintEl.textContent = t.hint;
}

function selectedTestWebhook() {
  const sel = $("#testWebhookSelect");
  return WEBHOOKS.find((w) => w.id === sel.value) || null;
}

async function populateTestWebhookSelect() {
  const sel = $("#testWebhookSelect");
  if (!WEBHOOKS.length) {
    sel.innerHTML = '<option value="">No webhooks — create one in the Webhooks tab</option>';
    updateWebhookUrl("");
    renderAlertTemplate(null, $("#alertTemplate"), $("#alertTemplateHint"));
    return;
  }
  const prev = sel.value;
  sel.innerHTML = WEBHOOKS.map((w) =>
    `<option value="${w.id}">${escapeHtml(w.name)} (${w.strategy})</option>`).join("");
  sel.value = WEBHOOKS.some((w) => w.id === prev) ? prev : WEBHOOKS[0].id;
  updateWebhookUrl(selectedTestWebhook()?.token);
  renderAlertTemplate(selectedTestWebhook(), $("#alertTemplate"), $("#alertTemplateHint"));
}

$("#testWebhookSelect").addEventListener("change", () => {
  updateWebhookUrl(selectedTestWebhook()?.token);
  renderAlertTemplate(selectedTestWebhook(), $("#alertTemplate"), $("#alertTemplateHint"));
});

$("#copyTemplate").addEventListener("click", () => {
  navigator.clipboard.writeText($("#alertTemplate").textContent);
  toast("Alert message copied", "success");
});

const PRESETS = {
  simple_buy: { action: "buy", symbol: "MNQ1!", qty: 2 },
  simple_sell: { action: "sell", symbol: "MNQ1!", qty: 2 },
  entry: {
    event: "entry", action: "sell", symbol: "MNQ1!", entry: 30267,
    sl: 30285.06839, tp1: 30261.57948, tp2: 30265.19316, tp3: 30247.0425,
    qty: 4.95623, risk_usd: 179.10204,
  },
  move_sl: {
    event: "tp1_hit", action: "move_sl", symbol: "MNQ1!", new_sl: 30266.01,
    message: "TP1 reached — SL moved to net-breakeven",
  },
  trail: {
    event: "tp2_hit", action: "trail_active", symbol: "MNQ1!",
    trail_ema: "ema9", trail_buffer: 0.15, message: "TP2 reached — trailing stop active",
  },
  close: {
    event: "tp3_hit", action: "close_all", symbol: "MNQ1!",
    exit_price: 30241.70425, pnl: 250.70285, message: "TP3 full kill — close all",
  },
  runner: {
    event: "runner_exit", action: "close_all", symbol: "MNQ1!",
    exit_price: 29761.94756, realized_R: 1.7,
    message: "Runner trailed out past TP3 — closed in profit",
  },
};

$$(".preset").forEach((btn) => {
  btn.addEventListener("click", () => {
    $("#testPayload").value = JSON.stringify(PRESETS[btn.dataset.preset], null, 2);
  });
});

$("#sendTestBtn").addEventListener("click", async () => {
  const wh = selectedTestWebhook();
  if (!wh) return toast("Create a webhook first (Webhooks tab)", "error");
  let payload;
  try { payload = JSON.parse($("#testPayload").value); }
  catch { return toast("Payload is not valid JSON", "error"); }
  const box = $("#testResult");
  try {
    const r = await api(`/api/webhooks/${wh.id}/test`, { method: "POST", body: JSON.stringify(payload) });
    box.textContent = JSON.stringify(r, null, 2);
    box.className = "result-box";
    toast("Signal processed", "success");
    refreshOrders(); refreshLogs(); refreshStatus();
  } catch (e) {
    box.textContent = "Error: " + e.message;
    box.className = "result-box";
    toast(e.message, "error");
  }
});

$("#refreshPositions").addEventListener("click", refreshPositions);
$("#refreshLogs").addEventListener("click", refreshLogs);

/* ----------------------------------------------------------- token accounts */
async function loadTokenAccounts() {
  try { renderTokenAccounts(await api("/api/token-accounts")); } catch (e) { /* ignore */ }
}

function tokenRow(a = {}) {
  const env = a.environment === "live" ? "live" : "demo";
  return `<tr>
    <td><input type="checkbox" class="switch ta-enabled" ${a.enabled ? "checked" : ""} /></td>
    <td><input class="ta-name" value="${escapeHtml(a.name || "")}" placeholder="Account 1" style="width:120px" /></td>
    <td><select class="ta-env">
      <option value="demo" ${env === "demo" ? "selected" : ""}>Demo</option>
      <option value="live" ${env === "live" ? "selected" : ""}>Live</option>
    </select></td>
    <td><input class="ta-access" value="${escapeHtml(a.access_token || "")}" placeholder="access token" type="password" autocomplete="off" /></td>
    <td><input class="ta-md" value="${escapeHtml(a.md_token || "")}" placeholder="check token (optional)" type="password" autocomplete="off" /></td>
    <td><input type="number" class="ta-mult" min="0.1" step="0.1" value="${a.qty_multiplier ?? 1}" style="width:70px" /></td>
    <td><button type="button" class="btn btn-ghost ta-del">✕</button></td>
  </tr>`;
}

function renderTokenAccounts(accounts) {
  const tbody = $("#tokenAccountsTable tbody");
  const list = accounts && accounts.length ? accounts : [{}];
  tbody.innerHTML = list.map(tokenRow).join("");
  tbody.querySelectorAll(".ta-del").forEach((b) =>
    b.addEventListener("click", () => b.closest("tr").remove()));
}

function collectTokenAccounts() {
  return [...$$("#tokenAccountsTable tbody tr")].map((tr) => ({
    enabled: tr.querySelector(".ta-enabled").checked,
    name: tr.querySelector(".ta-name").value.trim(),
    environment: tr.querySelector(".ta-env").value,
    access_token: tr.querySelector(".ta-access").value.trim(),
    md_token: tr.querySelector(".ta-md").value.trim(),
    qty_multiplier: Number(tr.querySelector(".ta-mult").value) || 1,
  })).filter((a) => a.name || a.access_token);
}

$("#addTokenRow").addEventListener("click", () => {
  $("#tokenAccountsTable tbody").insertAdjacentHTML("beforeend", tokenRow());
  const last = $("#tokenAccountsTable tbody tr:last-child .ta-del");
  if (last) last.addEventListener("click", () => last.closest("tr").remove());
});

$("#saveTokenAccountsBtn").addEventListener("click", async () => {
  try {
    await api("/api/token-accounts", { method: "POST", body: JSON.stringify(collectTokenAccounts()) });
    $("#tokenAccountsHint").textContent = "Saved ✓";
    setTimeout(() => ($("#tokenAccountsHint").textContent = ""), 2500);
    toast("Token accounts saved", "success");
    loadTokenAccounts();
    refreshStatus();
  } catch (e) { toast(e.message, "error"); }
});

/* --------------------------------------------------- trade accounts (on/off) */
async function loadTradeAccounts() {
  try { renderTradeAccounts(await api("/api/trade-accounts")); } catch (e) { /* ignore */ }
}

let KNOWN_ACCOUNTS = [];   // last-fetched trade-account overview, reused by the Webhooks tab

function renderTradeAccounts(accounts) {
  KNOWN_ACCOUNTS = accounts || [];
  const tbody = $("#tradeAccountsTable tbody");
  if (!accounts || !accounts.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty">No accounts yet — add a token above, then Discover / Refresh</td></tr>';
    return;
  }
  // Read-only: which accounts trade is chosen per webhook (Webhooks tab).
  tbody.innerHTML = accounts.map((a) => {
    const status = a.connected
      ? '<span class="pos">Connected</span>'
      : '<span class="neg">Not connected</span>';
    return `<tr>
      <td>${escapeHtml(a.token_name || "—")}</td>
      <td>${escapeHtml(a.spec || "—")}</td>
      <td>${(a.environment || "—").toUpperCase()}</td>
      <td>${status}</td></tr>`;
  }).join("");
}

$("#refreshTradeAccounts").addEventListener("click", connectAll);

/* --------------------------------------------------------------- webhooks */
let WEBHOOKS = [];

async function loadWebhooks() {
  try {
    WEBHOOKS = await api("/api/webhooks");
    renderWebhooks();
    populateTestWebhookSelect();
  } catch (e) { /* ignore */ }
}

function accountKey(tokenIdx, spec) { return `${tokenIdx}::${spec}`; }

function webhookAccountRows(webhook) {
  const selected = new Map(
    (webhook.accounts || []).map((a) => [accountKey(a.token_idx, a.spec), a])
  );
  if (!KNOWN_ACCOUNTS.length) {
    return '<tr><td colspan="5" class="empty">No accounts yet — add a login under Settings → Token Accounts, then Discover / Refresh</td></tr>';
  }
  return KNOWN_ACCOUNTS.map((a) => {
    const key = accountKey(a.token_idx, a.spec);
    const sel = selected.get(key);
    const enabled = sel ? sel.enabled : false;
    const mult = sel ? sel.qty_multiplier : 1;
    return `<tr data-token-idx="${a.token_idx}" data-spec="${escapeHtml(a.spec)}">
      <td><input type="checkbox" class="switch wh-acc-enabled" ${enabled ? "checked" : ""} /></td>
      <td>${escapeHtml(a.token_name || "—")}</td>
      <td>${escapeHtml(a.spec || "—")}</td>
      <td>${(a.environment || "—").toUpperCase()}</td>
      <td><input type="number" class="wh-acc-mult" min="0.1" step="0.1" value="${mult ?? 1}" style="width:80px" /></td>
    </tr>`;
  }).join("");
}

function webhookDetail(w) {
  const url = `${location.origin}/webhook/${w.token}`;
  const tmpl = alertMessageTemplate(w.strategy);
  return `
    <div class="row-detail wh-edit">
      <div class="grid grid-2">
        <label>Name <input class="wh-name" value="${escapeHtml(w.name)}" /></label>
        <label>Strategy
          <select class="wh-strategy">
            <option value="simple" ${w.strategy === "simple" ? "selected" : ""}>simple (buy/sell only)</option>
            <option value="bracket" ${w.strategy === "bracket" ? "selected" : ""}>bracket (entry + TP/SL)</option>
            <option value="ts_hunter" ${w.strategy === "ts_hunter" ? "selected" : ""}>TS-Hunter (signal + partial closes)</option>
          </select>
        </label>
        <label class="wh-default-qty-label" style="${w.strategy === "ts_hunter" ? "display:none" : ""}">Default qty (fallback if payload omits qty)
          <input class="wh-default-qty" type="number" min="1" value="${w.default_qty ?? 1}" />
        </label>
        <label class="wh-tp-qty-label" style="${w.strategy === "bracket" ? "" : "display:none"}">TP qty (bracket only)
          <input class="wh-tp-qty" type="number" min="1" value="${w.tp_qty ?? 1}" />
        </label>
      </div>
      <div class="url-box">
        <code class="wh-url">${url}</code>
        <button type="button" class="btn btn-ghost wh-copy">Copy</button>
      </div>
      <div class="card-head" style="margin-top:14px">
        <span class="hint">Alert message — paste into the TradingView alert's "Message" box</span>
        <button type="button" class="btn btn-ghost wh-copy-template">Copy</button>
      </div>
      <pre class="code wh-template">${escapeHtml(tmpl.json)}</pre>
      <p class="hint wh-template-hint">${tmpl.hint}</p>
      <table class="data-table wh-accounts-table">
        <thead><tr><th>Enabled</th><th>Login</th><th>Account</th><th>Env</th><th>Qty ×</th></tr></thead>
        <tbody>${webhookAccountRows(w)}</tbody>
      </table>
      <div class="form-actions">
        <button type="button" class="btn btn-primary wh-save">Save</button>
        <button type="button" class="btn btn-ghost wh-regen">Regenerate token</button>
        <button type="button" class="btn btn-ghost wh-delete">Delete</button>
        <span class="save-hint wh-hint"></span>
      </div>
    </div>`;
}

function webhookRow(w) {
  const url = `${location.origin}/webhook/${w.token}`;
  const accCount = (w.accounts || []).filter((a) => a.enabled).length;
  return `
  <tr class="wh-row" data-id="${w.id}">
    <td class="col-exp"><span class="row-exp">▸</span></td>
    <td><input type="checkbox" class="switch wh-enabled" ${w.enabled ? "checked" : ""} /></td>
    <td class="wh-name-cell">${escapeHtml(w.name)}</td>
    <td><span class="tag wh-strategy-tag">${w.strategy}</span></td>
    <td class="wh-acc-count">${accCount}</td>
    <td class="url-cell"><code class="wh-url-sm">${url}</code></td>
  </tr>
  <tr class="wh-detail hidden" data-id="${w.id}"><td colspan="6">${webhookDetail(w)}</td></tr>`;
}

function renderWebhooks() {
  const tbody = $("#webhooksTable tbody");
  if (!WEBHOOKS.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty">No webhooks yet — click "+ Add Webhook".</td></tr>';
    return;
  }
  tbody.innerHTML = WEBHOOKS.map(webhookRow).join("");
  WEBHOOKS.forEach((w) => wireWebhookRow(tbody, w.id));
}

function wireWebhookRow(tbody, id) {
  const row = tbody.querySelector(`tr.wh-row[data-id="${id}"]`);
  const detailRow = tbody.querySelector(`tr.wh-detail[data-id="${id}"]`);
  const detail = detailRow.querySelector(".wh-edit");

  // Expand/collapse when the row (not an interactive control) is clicked.
  row.addEventListener("click", (e) => {
    if (e.target.closest("input,button,select,textarea,code,a")) return;
    const open = detailRow.classList.toggle("hidden");
    row.classList.toggle("expanded", !open);
  });

  const strategySel = detail.querySelector(".wh-strategy");
  const tpLabel = detail.querySelector(".wh-tp-qty-label");
  const defaultQtyLabel = detail.querySelector(".wh-default-qty-label");
  strategySel.addEventListener("change", () => {
    tpLabel.style.display = strategySel.value === "bracket" ? "" : "none";
    defaultQtyLabel.style.display = strategySel.value === "ts_hunter" ? "none" : "";
    const t = alertMessageTemplate(strategySel.value);
    detail.querySelector(".wh-template").textContent = t.json;
    detail.querySelector(".wh-template-hint").textContent = t.hint;
  });

  detail.querySelector(".wh-copy-template").addEventListener("click", () => {
    navigator.clipboard.writeText(detail.querySelector(".wh-template").textContent);
    toast("Alert message copied", "success");
  });
  detail.querySelector(".wh-copy").addEventListener("click", () => {
    navigator.clipboard.writeText(detail.querySelector(".wh-url").textContent);
    toast("Webhook URL copied", "success");
  });

  detail.querySelector(".wh-save").addEventListener("click", async () => {
    const accounts = [...detail.querySelectorAll(".wh-accounts-table tbody tr[data-spec]")]
      .map((tr) => ({
        token_idx: Number(tr.dataset.tokenIdx),
        spec: tr.dataset.spec,
        enabled: tr.querySelector(".wh-acc-enabled").checked,
        qty_multiplier: Number(tr.querySelector(".wh-acc-mult").value) || 1,
      }))
      .filter((a) => a.enabled);
    const body = {
      name: detail.querySelector(".wh-name").value.trim() || "Untitled",
      enabled: row.querySelector(".wh-enabled").checked,
      strategy: strategySel.value,
      default_qty: Number(detail.querySelector(".wh-default-qty").value) || 1,
      tp_qty: Number(detail.querySelector(".wh-tp-qty").value) || 1,
      accounts,
    };
    try {
      const updated = await api(`/api/webhooks/${id}`, { method: "PUT", body: JSON.stringify(body) });
      const i = WEBHOOKS.findIndex((w) => w.id === id);
      if (i >= 0) WEBHOOKS[i] = updated;
      // Sync the summary row in place (keeps the detail open).
      row.querySelector(".wh-name-cell").textContent = updated.name;
      row.querySelector(".wh-strategy-tag").textContent = updated.strategy;
      row.querySelector(".wh-acc-count").textContent = (updated.accounts || []).filter((a) => a.enabled).length;
      const hint = detail.querySelector(".wh-hint");
      hint.textContent = "Saved ✓";
      setTimeout(() => (hint.textContent = ""), 2500);
      toast("Webhook saved", "success");
      populateTestWebhookSelect();
    } catch (e) { toast(e.message, "error"); }
  });

  detail.querySelector(".wh-regen").addEventListener("click", async () => {
    if (!confirm("Regenerate this webhook's token? The old URL will stop working — update your TradingView alert.")) return;
    try {
      const updated = await api(`/api/webhooks/${id}/regenerate-token`, { method: "POST" });
      const i = WEBHOOKS.findIndex((w) => w.id === id);
      if (i >= 0) WEBHOOKS[i] = updated;
      renderWebhooks();
      populateTestWebhookSelect();
      toast("Token regenerated", "success");
    } catch (e) { toast(e.message, "error"); }
  });

  detail.querySelector(".wh-delete").addEventListener("click", async () => {
    const name = detail.querySelector(".wh-name").value || "this webhook";
    if (!confirm(`Delete "${name}"? This cannot be undone.`)) return;
    try {
      await api(`/api/webhooks/${id}`, { method: "DELETE" });
      WEBHOOKS = WEBHOOKS.filter((w) => w.id !== id);
      renderWebhooks();
      populateTestWebhookSelect();
      toast("Webhook deleted", "success");
    } catch (e) { toast(e.message, "error"); }
  });
}

$("#addWebhookBtn").addEventListener("click", async () => {
  try {
    const wh = await api("/api/webhooks", {
      method: "POST",
      body: JSON.stringify({ name: `Strategy ${WEBHOOKS.length + 1}`, strategy: "simple", default_qty: 1, tp_qty: 1 }),
    });
    WEBHOOKS.push(wh);
    renderWebhooks();
    populateTestWebhookSelect();
    toast("Webhook created", "success");
  } catch (e) { toast(e.message, "error"); }
});

/* --------------------------------------------------------------- symbol map */
function symbolRow(tv = "", contract = "") {
  return `<tr>
    <td><input class="sm-tv" value="${escapeHtml(tv)}" placeholder="MNQ1!" /></td>
    <td><input class="sm-contract" value="${escapeHtml(contract)}" placeholder="MNQU6" /></td>
    <td><button type="button" class="btn btn-ghost sm-del">✕</button></td>
  </tr>`;
}

function renderSymbolMap(map) {
  const tbody = $("#symbolMapTable tbody");
  const entries = Object.entries(map || {});
  tbody.innerHTML = entries.length
    ? entries.map(([tv, c]) => symbolRow(tv, c)).join("")
    : symbolRow();
  tbody.querySelectorAll(".sm-del").forEach((b) =>
    b.addEventListener("click", () => b.closest("tr").remove()));
}

function collectSymbolMap() {
  const map = {};
  $$("#symbolMapTable tbody tr").forEach((tr) => {
    const tv = tr.querySelector(".sm-tv").value.trim();
    const c = tr.querySelector(".sm-contract").value.trim();
    if (tv && c) map[tv] = c;
  });
  return map;
}

$("#addSymbolRow").addEventListener("click", () => {
  $("#symbolMapTable tbody").insertAdjacentHTML("beforeend", symbolRow());
  const last = $("#symbolMapTable tbody tr:last-child .sm-del");
  if (last) last.addEventListener("click", () => last.closest("tr").remove());
});

$("#saveSymbolMapBtn").addEventListener("click", async () => {
  try {
    await api("/api/settings", { method: "POST", body: JSON.stringify({ symbol_map: collectSymbolMap() }) });
    $("#symbolMapHint").textContent = "Saved ✓";
    setTimeout(() => ($("#symbolMapHint").textContent = ""), 2500);
    toast("Symbol mapping saved", "success");
  } catch (e) { toast(e.message, "error"); }
});

$("#healthCheckBtn").addEventListener("click", async () => {
  toast("Checking connections…");
  try {
    const r = await api("/api/health", { method: "GET" });
    renderSessions(r.sessions || []);
    const ok = (r.sessions || []).filter((x) => x.connected).length;
    toast(`${ok}/${(r.sessions || []).length} account(s) healthy`, ok ? "success" : "error");
  } catch (e) { toast(e.message, "error"); }
});

/* --------------------------------------------------------------- simulator */
let SCENARIOS = [];
let simIndex = 0;     // index of the next step to run

async function loadScenarios() {
  try {
    SCENARIOS = await api("/api/scenarios");
    const sel = $("#scenarioSelect");
    sel.innerHTML = SCENARIOS.map((s, i) => `<option value="${i}">${s.name}</option>`).join("");
    renderScenario();
  } catch (e) { /* ignore */ }
}

function currentScenario() { return SCENARIOS[$("#scenarioSelect").value || 0]; }

function renderScenario() {
  const sc = currentScenario();
  if (!sc) return;
  simIndex = 0;
  $("#scenarioDesc").textContent = sc.description;
  const list = $("#simSteps");
  list.innerHTML = sc.steps.map((step, i) => `
    <li class="sim-step" data-i="${i}">
      <div class="sim-step-head">
        <span class="sim-step-num">${i + 1}</span>
        <span class="sim-step-label">${escapeHtml(step.label)}</span>
        <span class="sim-step-status"></span>
      </div>
      <div class="sim-step-body">
        <pre class="sim-signal">${escapeHtml(JSON.stringify(step.signal, null, 2))}</pre>
        <pre class="sim-result hidden"></pre>
      </div>
    </li>`).join("");
  list.querySelectorAll(".sim-step-head").forEach((h) =>
    h.addEventListener("click", () => h.parentElement.classList.toggle("open")));
  updateSimProgress();
  refreshSimState();
}

function updateSimProgress() {
  const sc = currentScenario();
  $("#simProgress").textContent = sc ? `${simIndex} / ${sc.steps.length} executed` : "";
  $$("#simSteps .sim-step").forEach((el, i) => {
    el.classList.toggle("current", i === simIndex);
  });
}

async function runStep(i) {
  const sc = currentScenario();
  if (!sc || i >= sc.steps.length) return false;
  const step = sc.steps[i];
  const el = $(`#simSteps .sim-step[data-i="${i}"]`);
  const statusEl = el.querySelector(".sim-step-status");
  const resultEl = el.querySelector(".sim-result");
  statusEl.textContent = "running…";
  try {
    const r = await api("/api/simulate", { method: "POST", body: JSON.stringify(step.signal) });
    el.classList.remove("failed"); el.classList.add("done");
    const n = (r.orders || []).length;
    statusEl.textContent = r.status === "ok"
      ? (n ? `✓ ${n} order(s)` : "✓ " + (r.action || "ok"))
      : "• " + (r.reason || r.status);
    resultEl.textContent = JSON.stringify(r, null, 2);
    resultEl.classList.remove("hidden");
    return true;
  } catch (e) {
    el.classList.add("failed");
    statusEl.textContent = "✗ " + e.message;
    resultEl.textContent = "Error: " + e.message;
    resultEl.classList.remove("hidden");
    return false;
  } finally {
    refreshSimState();
  }
}

$("#simStep").addEventListener("click", async () => {
  const sc = currentScenario();
  if (!sc || simIndex >= sc.steps.length) { toast("Scenario complete — reset to run again"); return; }
  const ok = await runStep(simIndex);
  if (ok) { simIndex++; updateSimProgress(); }
});

$("#simRunAll").addEventListener("click", async () => {
  const sc = currentScenario();
  if (!sc) return;
  $("#simRunAll").disabled = true;
  for (; simIndex < sc.steps.length; simIndex++) {
    updateSimProgress();
    const ok = await runStep(simIndex);
    if (!ok) break;
    await new Promise((r) => setTimeout(r, 700));
  }
  updateSimProgress();
  $("#simRunAll").disabled = false;
  toast("Simulation finished", "success");
});

$("#simReset").addEventListener("click", async () => {
  try { await api("/api/simulate/reset", { method: "POST" }); } catch (e) { /* ignore */ }
  renderScenario();
  toast("Simulation reset");
});

$("#scenarioSelect").addEventListener("change", async () => {
  try { await api("/api/simulate/reset", { method: "POST" }); } catch (e) { /* ignore */ }
  renderScenario();
});

$("#simRefresh").addEventListener("click", refreshSimState);

async function refreshSimState() {
  try {
    const st = await api("/api/simulate/state");
    const pb = $("#simPositions tbody");
    pb.innerHTML = (st.positions || []).length
      ? st.positions.map((p) => `<tr><td>${p.symbol}</td>
          <td class="${p.netPos >= 0 ? "pos" : "neg"}">${p.netPos}</td>
          <td>${p.netPrice}</td></tr>`).join("")
      : '<tr><td colspan="3" class="empty">Flat</td></tr>';
    const wb = $("#simWorking tbody");
    wb.innerHTML = (st.working_orders || []).length
      ? st.working_orders.map((o) => {
          const side = (o.action || "").toLowerCase();
          return `<tr><td>${o.id}</td>
            <td><span class="tag ${side}">${o.action}</span></td>
            <td>${o.qty}</td><td>${o.order_type}</td>
            <td>${o.price ?? o.stop_price ?? "—"}</td></tr>`;
        }).join("")
      : '<tr><td colspan="5" class="empty">None</td></tr>';
  } catch (e) { /* ignore */ }
}

/* ---------------------------------------------------- Discord Signals */
let DS_CHANNELS = [];

const DS_TEST_PRESETS = {
  entry: {
    title: "AkSniper 🎯 · SELL MNQ",
    fields: [{ name: "Contracts", value: "3" }, { name: "Entry", value: "20450.25" }, { name: "Time", value: "10:31" }],
  },
  update: {
    title: "AkSniper 🎯 · Stop / target moved · MNQ",
    fields: [{ name: "Stop", value: "20440.0 → 20450.0" }, { name: "Target", value: "20500.0 → 20520.5" }, { name: "Position", value: "3" }],
  },
  close: {
    title: "Closed MNQ · +90.75 pts",
    fields: [{ name: "P&L", value: "+$181.50" }, { name: "Move", value: "+90.75" }, { name: "Exit", value: "20541.0" }, { name: "Held", value: "12m" }],
  },
  junk: {
    title: "Brand-new message type nobody expected",
    fields: [{ name: "Whatever", value: "???" }],
  },
};

async function loadDiscordConfig() {
  try {
    const c = await api("/api/discord/config");
    $("#dsEnabled").checked = !!c.discord_enabled;
    $("#dsDryRunToggle").checked = !!c.discord_dry_run;
    $("#dsToken").value = c.discord_user_token || "";
    DS_CHANNELS = c.discord_channels || [];
    renderDiscordChannels();
  } catch (e) { /* ignore */ }
}

function dsWebhookOptions(selectedId) {
  const opts = (WEBHOOKS || []).map((w) =>
    `<option value="${w.id}" ${w.id === selectedId ? "selected" : ""}>${escapeHtml(w.name)} (${w.strategy})</option>`
  ).join("");
  return `<option value="" ${selectedId ? "" : "selected"}>Custom URL…</option>` + opts;
}

function dsTargetRow(t = {}) {
  const wid = t.webhook_id || "";
  const isCustom = !wid;
  return `<tr class="ds-target">
    <td><input type="checkbox" class="switch ds-t-enabled" ${t.enabled ? "checked" : ""} /></td>
    <td><input class="ds-t-label" value="${escapeHtml(t.label || "")}" placeholder="(optional)" style="width:110px" /></td>
    <td>
      <select class="ds-t-webhook" style="min-width:200px">${dsWebhookOptions(wid)}</select>
      <div class="ds-t-custom ${isCustom ? "" : "hidden"}" style="margin-top:6px">
        <input class="ds-t-url" value="${escapeHtml(t.url || "")}" placeholder="https://…/webhook/&lt;token&gt; or external URL" style="width:100%;min-width:240px" />
        <input type="password" class="ds-t-secret" value="${escapeHtml(t.secret || "")}" placeholder="X-Webhook-Secret (optional)" autocomplete="off" style="width:100%;margin-top:6px" />
      </div>
    </td>
    <td><button type="button" class="btn btn-ghost ds-t-del">✕</button></td>
  </tr>`;
}

function dsChannelRows(c = {}, idx = 0) {
  const targets = (c.targets || []).map(dsTargetRow).join("");
  const count = (c.targets || []).length;
  return `
  <tr class="ds-crow" data-idx="${idx}">
    <td class="col-exp"><span class="row-exp">▸</span></td>
    <td><input type="checkbox" class="switch ds-c-enabled" ${c.enabled ? "checked" : ""} /></td>
    <td><input class="ds-c-label" value="${escapeHtml(c.label || "")}" placeholder="Signal channel" /></td>
    <td><input class="ds-c-id" value="${escapeHtml(c.id || "")}" placeholder="123456789012345678" style="width:100%;min-width:170px" /></td>
    <td class="ds-c-count">${count}</td>
    <td><button type="button" class="btn btn-ghost ds-c-del">✕</button></td>
  </tr>
  <tr class="ds-cdetail hidden" data-idx="${idx}"><td colspan="6">
    <div class="row-detail">
      <table class="data-table ds-targets">
        <thead><tr><th>On</th><th>Label</th><th>Target webhook</th><th></th></tr></thead>
        <tbody>${targets || '<tr class="ds-empty-row"><td colspan="4" class="empty">No targets — add one.</td></tr>'}</tbody>
      </table>
      <div class="form-actions"><button type="button" class="btn btn-ghost ds-add-target">+ Add target</button></div>
    </div>
  </td></tr>`;
}

function renderDiscordChannels() {
  const tbody = $("#dsChannelsTable tbody");
  if (!DS_CHANNELS.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty">No channels yet — click "+ Add channel".</td></tr>';
    return;
  }
  tbody.innerHTML = DS_CHANNELS.map(dsChannelRows).join("");
  DS_CHANNELS.forEach((c, i) => wireDsChannelRow(tbody, i));
}

function wireDsChannelRow(tbody, idx) {
  const row = tbody.querySelector(`tr.ds-crow[data-idx="${idx}"]`);
  const detailRow = tbody.querySelector(`tr.ds-cdetail[data-idx="${idx}"]`);
  const tgtBody = detailRow.querySelector(".ds-targets tbody");
  const updateCount = () => {
    row.querySelector(".ds-c-count").textContent = tgtBody.querySelectorAll(".ds-target").length;
  };

  row.addEventListener("click", (e) => {
    if (e.target.closest("input,button,select,textarea,code,a")) return;
    const open = detailRow.classList.toggle("hidden");
    row.classList.toggle("expanded", !open);
  });
  row.querySelector(".ds-c-del").addEventListener("click", () => { row.remove(); detailRow.remove(); });

  detailRow.querySelector(".ds-add-target").addEventListener("click", () => {
    const empty = tgtBody.querySelector(".ds-empty-row");
    if (empty) empty.remove();
    tgtBody.insertAdjacentHTML("beforeend", dsTargetRow({ enabled: true }));
    wireDiscordTarget(tgtBody.lastElementChild, updateCount);
    updateCount();
  });
  tgtBody.querySelectorAll(".ds-target").forEach((r) => wireDiscordTarget(r, updateCount));
}

function wireDiscordTarget(row, onChange) {
  const sel = row.querySelector(".ds-t-webhook");
  const custom = row.querySelector(".ds-t-custom");
  if (sel && custom) {
    sel.addEventListener("change", () => custom.classList.toggle("hidden", sel.value !== ""));
  }
  row.querySelector(".ds-t-del").addEventListener("click", () => { row.remove(); if (onChange) onChange(); });
}

function collectDiscordChannels() {
  const tbody = $("#dsChannelsTable tbody");
  return [...tbody.querySelectorAll("tr.ds-crow")].map((row) => {
    const detailRow = tbody.querySelector(`tr.ds-cdetail[data-idx="${row.dataset.idx}"]`);
    const targets = detailRow
      ? [...detailRow.querySelectorAll(".ds-target")].map((r) => {
          const label = r.querySelector(".ds-t-label").value.trim();
          const enabled = r.querySelector(".ds-t-enabled").checked;
          const wid = r.querySelector(".ds-t-webhook").value;
          if (wid) return { label, webhook_id: wid, enabled };
          return {
            label, enabled,
            url: r.querySelector(".ds-t-url").value.trim(),
            secret: r.querySelector(".ds-t-secret").value,
          };
        }).filter((t) => t.webhook_id || t.url)
      : [];
    return {
      id: row.querySelector(".ds-c-id").value.trim(),
      label: row.querySelector(".ds-c-label").value.trim(),
      enabled: row.querySelector(".ds-c-enabled").checked,
      targets,
    };
  }).filter((c) => c.id);
}

$("#dsAddChannel").addEventListener("click", () => {
  DS_CHANNELS = collectDiscordChannels();
  DS_CHANNELS.push({ label: "", id: "", enabled: true, targets: [] });
  renderDiscordChannels();
});

$("#dsSave").addEventListener("click", async () => {
  const hint = $("#dsSaveHint");
  const body = {
    discord_enabled: $("#dsEnabled").checked,
    discord_dry_run: $("#dsDryRunToggle").checked,
    discord_user_token: $("#dsToken").value,
    discord_channels: collectDiscordChannels(),
  };
  try {
    const c = await api("/api/discord/config", { method: "POST", body: JSON.stringify(body) });
    DS_CHANNELS = c.discord_channels || [];
    $("#dsToken").value = c.discord_user_token || "";
    renderDiscordChannels();
    hint.textContent = "Saved ✓"; hint.className = "save-hint ok";
    toast("Discord config saved", "success");
    refreshDiscordStatus();
  } catch (e) {
    hint.textContent = e.message; hint.className = "save-hint err";
    toast(e.message, "error");
  }
  setTimeout(() => { hint.textContent = ""; }, 3000);
});

async function refreshDiscordStatus() {
  try {
    const s = await api("/api/discord/status");
    const stateEl = $("#dsState");
    const label = { connected: "Connected", connecting: "Connecting…", disabled: "Disabled",
      error: "Error", library_missing: "No library", stopped: "Stopped" }[s.state] || s.state;
    const full = label + (s.user ? ` (${s.user})` : "");
    stateEl.textContent = full;
    stateEl.className = "status-v " + (s.state === "connected" ? "on" : s.state === "error" ? "off" : "");
    $("#dsDryRun").textContent = s.dry_run ? "ON" : "off";
    $("#dsWatched").textContent = (s.watched_channels || []).length;
    $("#dsLibWarn").classList.toggle("hidden", s.library_available);
    const bar = $("#dsBarState");
    if (bar) {
      bar.textContent = s.enabled ? full : "off";
      bar.className = "status-v " + (s.state === "connected" ? "on" : s.state === "error" ? "off" : "");
    }
  } catch (e) { /* ignore */ }
}

function dsFeedLine(ev) {
  const time = fmtTime(ev.ts);
  const src = ev.source ? `<span class="tag">${escapeHtml(ev.source)}</span>` : "";
  if (ev.kind === "unrecognized") {
    return `<div class="log-line"><span class="lt">${time}</span>
      <span class="lv" style="color:var(--warn,#e0a800)">UNKNOWN</span>
      <code>${escapeHtml(ev.channel_label || "")} — ${escapeHtml((ev.raw && ev.raw.title) || "")}</code></div>`;
  }
  const sig = ev.signal || {};
  const dry = ev.dry_run ? '<span class="tag sim">DRY</span>' : "";
  const ok = (ev.targets || []).filter((t) => t.ok).length;
  const total = (ev.targets || []).length;
  const failed = (ev.targets || []).filter((t) => t.ok === false).length;
  const targetTxt = ev.dry_run
    ? `${total} target(s) skipped`
    : `${ok}/${total} sent${failed ? `, ${failed} failed` : ""}`;
  const lat = ev.latency_ms != null ? ` · ${ev.latency_ms}ms` : "";
  const side = sig.side ? ` ${sig.side}` : "";
  return `<div class="log-line"><span class="lt">${time}</span>
    <span class="lv">${escapeHtml(sig.event_type || "signal")}</span>
    <code>${escapeHtml(ev.channel_label || "")} · ${escapeHtml(sig.symbol || "?")}${side} ${src}${dry} — ${targetTxt}${lat}</code></div>`;
}

function dsRenderFeed(events) {
  const box = $("#dsFeed");
  if (!events.length) { box.innerHTML = '<div class="empty">No signals yet.</div>'; return; }
  box.innerHTML = events.map(dsFeedLine).join("");
}

async function loadDiscordSignals() {
  try { dsRenderFeed(await api("/api/discord/signals")); } catch (e) { /* ignore */ }
}

let DS_FEED = [];
function connectDiscordStream() {
  let es;
  try { es = new EventSource("/api/discord/stream"); }
  catch (e) { return; }
  es.onopen = () => {
    $("#dsStreamDot").className = "dot on";
    $("#dsStreamText").textContent = "live";
  };
  es.onerror = () => {
    $("#dsStreamDot").className = "dot";
    $("#dsStreamText").textContent = "reconnecting…";
    // EventSource auto-reconnects; nothing to do.
  };
  es.onmessage = (msg) => {
    try {
      const ev = JSON.parse(msg.data);
      DS_FEED.unshift(ev);
      DS_FEED = DS_FEED.slice(0, 200);
      dsRenderFeed(DS_FEED);
    } catch (e) { /* ignore */ }
  };
}

function dsLoadTestPreset() {
  const p = DS_TEST_PRESETS[$("#dsTestPreset").value] || DS_TEST_PRESETS.entry;
  $("#dsTestEmbed").value = JSON.stringify(p, null, 2);
}
$("#dsTestPreset").addEventListener("change", dsLoadTestPreset);

$("#dsTestSend").addEventListener("click", async () => {
  const box = $("#dsTestResult");
  let embed;
  try { embed = JSON.parse($("#dsTestEmbed").value); }
  catch { return toast("Embed is not valid JSON", "error"); }
  const channel_id = $("#dsTestChannel").value.trim();
  if (!channel_id) return toast("Enter a channel ID", "error");
  try {
    const r = await api("/api/discord/test", {
      method: "POST",
      body: JSON.stringify({ channel_id, embed, force: true }),
    });
    box.textContent = JSON.stringify(r, null, 2);
    toast("Test signal sent", "success");
    loadDiscordSignals();
  } catch (e) {
    box.textContent = "Error: " + e.message;
    toast(e.message, "error");
  }
});

/* ----------------------------------------------------- bookmarklets */
// No-install alternative to the extension: drag to the bookmarks bar, click on
// the site. Discord deletes window.localStorage in the page, so we read it from
// a fresh same-origin iframe. Tradovate keeps tokens in normal storage.
const BOOKMARKLETS = {
  discord:
    "javascript:%28function%28%29%7Btry%7Bvar%20f%3Ddocument.createElement%28%27iframe%27%29%3Bdocument.body.appendChild%28f%29%3Bvar%20r%3Df.contentWindow.localStorage.getItem%28%27token%27%29%3Bf.remove%28%29%3Bvar%20v%3Dr%3Fr.replace%28%2F%5E%22%2B%7C%22%2B%24%2Fg%2C%27%27%29%3Anull%3Bif%28v%29%7Bif%28navigator.clipboard%29navigator.clipboard.writeText%28v%29%3Bwindow.prompt%28%27Discord%20user%20token%20%28copied%29%3A%27%2Cv%29%3B%7Delse%7Balert%28%27No%20Discord%20token%20found.%20Log%20in%20on%20discord.com%20and%20try%20again.%27%29%3B%7D%7Dcatch%28e%29%7Balert%28%27Could%20not%20read%20token%3A%20%27%2Be%29%3B%7D%7D%29%28%29%3B",
  tradovate:
    "javascript:%28function%28%29%7Bfunction%20g%28k%29%7Btry%7Breturn%20localStorage.getItem%28k%29%7C%7CsessionStorage.getItem%28k%29%7Dcatch%28e%29%7Breturn%20null%7D%7Dfunction%20u%28x%29%7Breturn%20x%3Fx.replace%28%2F%5E%22%2B%7C%22%2B%24%2Fg%2C%27%27%29%3Ax%7Dvar%20t%3Du%28g%28%27token%27%29%29%2Cc%3Du%28g%28%27checkToken%27%29%29%3Bif%28navigator.clipboard%26%26t%29navigator.clipboard.writeText%28t%29%3Bwindow.prompt%28%27Tradovate%20token%20%28copied%29%3A%27%2Ct%7C%7C%27%28not%20found%29%27%29%3Bwindow.prompt%28%27Tradovate%20checkToken%3A%27%2Cc%7C%7C%27%28not%20found%29%27%29%3B%7D%29%28%29%3B",
};

function setupBookmarklets() {
  const map = { bmDiscord: "discord", bmTradovate: "tradovate" };
  for (const [id, key] of Object.entries(map)) {
    const a = document.getElementById(id);
    if (a) {
      a.setAttribute("href", BOOKMARKLETS[key]);
      a.addEventListener("click", (e) => {
        e.preventDefault();
        toast("Drag this to your bookmarks bar, then click it on the site.", "");
      });
    }
    const copy = document.getElementById(id + "Copy");
    if (copy) {
      copy.addEventListener("click", () => {
        navigator.clipboard.writeText(decodeURIComponent(BOOKMARKLETS[key]));
        toast("Bookmarklet code copied", "success");
      });
    }
  }
}

/* --------------------------------------------------------------- boot */
async function boot() {
  await loadSettings();
  refreshStatus();
  refreshOrders();
  refreshLogs();
  checkUpdate();
  loadTokenAccounts();
  await loadTradeAccounts();   // populates KNOWN_ACCOUNTS before webhook cards render
  await loadWebhooks();
  loadScenarios();
  $("#testPayload").value = JSON.stringify(PRESETS.simple_buy, null, 2);

  // Discord signal module
  loadDiscordConfig();
  refreshDiscordStatus();
  loadDiscordSignals();
  connectDiscordStream();
  dsLoadTestPreset();
  setupBookmarklets();
  loadAccount();

  setInterval(refreshStatus, 5000);
  setInterval(refreshOrders, 7000);
  setInterval(refreshLogs, 8000);
  setInterval(refreshDiscordStatus, 5000);
}
boot();
