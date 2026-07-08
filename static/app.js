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

/* --------------------------------------------------------------- tabs */
$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    $$(".tab").forEach((t) => t.classList.remove("active"));
    $$(".panel").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    $("#tab-" + tab.dataset.tab).classList.add("active");
  });
});

/* ----------------------------------------------------- collapsible cards */
// Every .card becomes an accordion (collapsed by default) except small stat
// tiles, the guide's section dividers, and its intro card (holds the TOC).
// Safe to call repeatedly (e.g. after re-rendering the Webhooks list) —
// already-wired cards are skipped via the collapsibleInit marker.
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

    const toggle = document.createElement("span");
    toggle.className = "card-toggle";
    toggle.textContent = "▸";
    head.appendChild(toggle);

    head.addEventListener("click", (e) => {
      if (e.target.closest("button, a, input, select, textarea, label")) return;
      card.classList.toggle("collapsed");
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

makeCardsCollapsible();

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
    te.className = "stat-value " + (trading ? "on" : "off");
    $("#statEnv").textContent = total ? `${con}/${total} login${total === 1 ? "" : "s"}` : "—";

    const ta = s.trade_accounts || [];
    const taOn = ta.filter((a) => a.enabled).length;
    $("#statAccount").textContent = ta.length ? `${taOn}/${ta.length} on` : "—";

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
    else if (key === "allowed_symbols") el.value = (val || []).join(", ");
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
    } else if (el.name === "allowed_symbols") {
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

/**
 * TradingView alert-message JSON for a given strategy type, using TradingView's
 * own placeholders ({{strategy.order.action}}, {{strategy.order.contracts}},
 * {{ticker}}, {{strategy.order.price}}) so it can be pasted straight into the
 * alert's Message box. Numeric fields are unquoted so the substituted value
 * stays a JSON number, not a string.
 */
function alertMessageTemplate(strategy) {
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
    tbody.innerHTML = '<tr><td colspan="6" class="empty">No accounts yet — add a token above, then Discover / Refresh</td></tr>';
    return;
  }
  tbody.innerHTML = accounts.map((a) => {
    const conn = a.connected;
    const status = conn
      ? '<span class="pos">Connected</span>'
      : '<span class="neg">Not connected</span>';
    return `<tr data-token-idx="${a.token_idx}" data-spec="${escapeHtml(a.spec)}" data-id="${a.id}">
      <td><input type="checkbox" class="switch ta-exec" ${a.enabled ? "checked" : ""} /></td>
      <td>${escapeHtml(a.token_name || "—")}</td>
      <td>${escapeHtml(a.spec || "—")}</td>
      <td>${(a.environment || "—").toUpperCase()}</td>
      <td><input type="number" class="ta-execmult" min="0.1" step="0.1" value="${a.qty_multiplier ?? 1}" style="width:70px" /></td>
      <td>${status}</td></tr>`;
  }).join("");
}

function collectTradeAccounts() {
  return [...$$("#tradeAccountsTable tbody tr[data-spec]")].map((tr) => ({
    token_idx: Number(tr.dataset.tokenIdx),
    spec: tr.dataset.spec,
    id: Number(tr.dataset.id) || 0,
    enabled: tr.querySelector(".ta-exec").checked,
    qty_multiplier: Number(tr.querySelector(".ta-execmult").value) || 1,
  }));
}

$("#saveTradeAccountsBtn").addEventListener("click", async () => {
  try {
    renderTradeAccounts(await api("/api/trade-accounts",
      { method: "POST", body: JSON.stringify(collectTradeAccounts()) }));
    $("#tradeAccountsHint").textContent = "Saved ✓";
    setTimeout(() => ($("#tradeAccountsHint").textContent = ""), 2500);
    toast("Execution settings saved", "success");
    refreshStatus();
  } catch (e) { toast(e.message, "error"); }
});

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

function webhookCard(w) {
  const url = `${location.origin}/webhook/${w.token}`;
  const tmpl = alertMessageTemplate(w.strategy);
  return `
  <div class="card wh-card" data-id="${w.id}">
    <div class="card-head">
      <h2>${escapeHtml(w.name)}</h2>
      <label class="switch-row" style="margin:0">
        <span>Enabled</span>
        <input type="checkbox" class="switch wh-enabled" ${w.enabled ? "checked" : ""} />
      </label>
    </div>
    <div class="grid grid-2">
      <label>Name <input class="wh-name" value="${escapeHtml(w.name)}" /></label>
      <label>Strategy
        <select class="wh-strategy">
          <option value="simple" ${w.strategy === "simple" ? "selected" : ""}>simple (buy/sell only)</option>
          <option value="bracket" ${w.strategy === "bracket" ? "selected" : ""}>bracket (entry + TP/SL)</option>
        </select>
      </label>
      <label>Default qty (fallback if payload omits qty)
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

function renderWebhooks() {
  const list = $("#webhooksList");
  if (!WEBHOOKS.length) {
    list.innerHTML = '<div class="card"><p class="empty">No webhooks yet — click "+ Add Webhook" above.</p></div>';
    return;
  }
  list.innerHTML = WEBHOOKS.map(webhookCard).join("");
  list.querySelectorAll(".wh-card").forEach(wireWebhookCard);
  makeCardsCollapsible(list);
}

function wireWebhookCard(card) {
  const id = card.dataset.id;
  const strategySel = card.querySelector(".wh-strategy");
  const tpLabel = card.querySelector(".wh-tp-qty-label");
  strategySel.addEventListener("change", () => {
    tpLabel.style.display = strategySel.value === "bracket" ? "" : "none";
    const t = alertMessageTemplate(strategySel.value);
    card.querySelector(".wh-template").textContent = t.json;
    card.querySelector(".wh-template-hint").textContent = t.hint;
  });

  card.querySelector(".wh-copy-template").addEventListener("click", () => {
    navigator.clipboard.writeText(card.querySelector(".wh-template").textContent);
    toast("Alert message copied", "success");
  });

  card.querySelector(".wh-copy").addEventListener("click", () => {
    navigator.clipboard.writeText(card.querySelector(".wh-url").textContent);
    toast("Webhook URL copied", "success");
  });

  card.querySelector(".wh-save").addEventListener("click", async () => {
    const accounts = [...card.querySelectorAll(".wh-accounts-table tbody tr[data-spec]")]
      .map((tr) => ({
        token_idx: Number(tr.dataset.tokenIdx),
        spec: tr.dataset.spec,
        enabled: tr.querySelector(".wh-acc-enabled").checked,
        qty_multiplier: Number(tr.querySelector(".wh-acc-mult").value) || 1,
      }))
      .filter((a) => a.enabled);
    const body = {
      name: card.querySelector(".wh-name").value.trim() || "Untitled",
      enabled: card.querySelector(".wh-enabled").checked,
      strategy: strategySel.value,
      default_qty: Number(card.querySelector(".wh-default-qty").value) || 1,
      tp_qty: Number(card.querySelector(".wh-tp-qty").value) || 1,
      accounts,
    };
    try {
      const updated = await api(`/api/webhooks/${id}`, { method: "PUT", body: JSON.stringify(body) });
      const i = WEBHOOKS.findIndex((w) => w.id === id);
      if (i >= 0) WEBHOOKS[i] = updated;
      const hint = card.querySelector(".wh-hint");
      hint.textContent = "Saved ✓";
      setTimeout(() => (hint.textContent = ""), 2500);
      toast("Webhook saved", "success");
      populateTestWebhookSelect();
    } catch (e) { toast(e.message, "error"); }
  });

  card.querySelector(".wh-regen").addEventListener("click", async () => {
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

  card.querySelector(".wh-delete").addEventListener("click", async () => {
    const name = card.querySelector(".wh-name").value || "this webhook";
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

  setInterval(refreshStatus, 5000);
  setInterval(refreshOrders, 7000);
  setInterval(refreshLogs, 8000);
}
boot();
