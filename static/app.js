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

/* --------------------------------------------------------------- status */
async function refreshStatus() {
  try {
    const s = await api("/api/status");
    $("#versionText").textContent = "v" + s.version;

    const conn = s.connection || {};
    $("#connDot").className = "dot" + (conn.connected ? " on" : "");
    $("#connText").textContent = conn.connected ? "Connected" : "Disconnected";

    const trading = s.trading_enabled;
    const te = $("#statTrading");
    te.textContent = trading ? "ENABLED" : "DISABLED";
    te.className = "stat-value " + (trading ? "on" : "off");
    $("#statEnv").textContent = (conn.environment || "—").toUpperCase();
    $("#statAccount").textContent = conn.account_spec || "—";

    renderActive(s.active_trades || {});
  } catch (e) { /* status polling is best-effort */ }
}

function renderActive(trades) {
  const tbody = $("#activeTable tbody");
  const keys = Object.keys(trades);
  if (!keys.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty">None</td></tr>';
    return;
  }
  tbody.innerHTML = keys.map((k) => {
    const t = trades[k];
    return `<tr><td>${k}</td><td>${t.contract}</td>
      <td><span class="tag ${t.side}">${t.side.toUpperCase()}</span></td>
      <td>${t.qty}</td><td>${t.sl_order_id || "—"}</td>
      <td>${(t.tp_order_ids || []).join(", ") || "—"}</td></tr>`;
  }).join("");
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
      return `<tr><td>${fmtTime(o.ts)}</td>
        <td><span class="tag ${sideClass}">${o.action}</span></td>
        <td>${o.symbol}</td><td>${o.qty}</td><td>${o.order_type}</td>
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
      tbody.innerHTML = '<tr><td colspan="4" class="empty">No positions</td></tr>';
      return;
    }
    tbody.innerHTML = positions.map((p) => {
      const net = p.netPos ?? 0;
      const pnl = p.openPL ?? p.realizedPL ?? 0;
      const pnlClass = pnl >= 0 ? "pos" : "neg";
      return `<tr><td>${p.contractId ?? p.symbol ?? "—"}</td>
        <td>${net}</td><td>${p.netPrice ?? "—"}</td>
        <td class="${pnlClass}">${Number(pnl).toFixed(2)}</td></tr>`;
    }).join("");
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="4" class="empty">${e.message}</td></tr>`;
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
  updateWebhookUrl(s.webhook_secret);
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
    updateWebhookUrl(payload.webhook_secret);
    refreshStatus();
  } catch (err) { toast(err.message, "error"); }
});

$("#connectBtn").addEventListener("click", async () => {
  toast("Connecting…");
  try {
    const r = await api("/api/connect", { method: "POST" });
    toast("Connected: " + (r.account_spec || "ok"), "success");
    loadSettings();
    refreshStatus();
  } catch (e) { toast("Connect failed: " + e.message, "error"); }
});

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
function updateWebhookUrl(secret) {
  const s = secret || "your-secret";
  const url = `${location.origin}/webhook/${s}`;
  $("#webhookUrl").textContent = url;
  const guideUrl = $("#guideUrl");
  if (guideUrl) guideUrl.textContent = url;
}

$("#copyUrl").addEventListener("click", () => {
  navigator.clipboard.writeText($("#webhookUrl").textContent);
  toast("Webhook URL copied", "success");
});

const PRESETS = {
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
};

$$(".preset").forEach((btn) => {
  btn.addEventListener("click", () => {
    $("#testPayload").value = JSON.stringify(PRESETS[btn.dataset.preset], null, 2);
  });
});

$("#sendTestBtn").addEventListener("click", async () => {
  let payload;
  try { payload = JSON.parse($("#testPayload").value); }
  catch { return toast("Payload is not valid JSON", "error"); }
  const box = $("#testResult");
  try {
    const r = await api("/api/webhook-test", { method: "POST", body: JSON.stringify(payload) });
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

/* --------------------------------------------------------------- boot */
async function boot() {
  await loadSettings();
  refreshStatus();
  refreshOrders();
  refreshLogs();
  checkUpdate();
  $("#testPayload").value = JSON.stringify(PRESETS.entry, null, 2);

  setInterval(refreshStatus, 5000);
  setInterval(refreshOrders, 7000);
  setInterval(refreshLogs, 8000);
}
boot();
