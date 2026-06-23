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

    renderHealth(conn);
    renderActive(s.active_trades || {});
  } catch (e) { /* status polling is best-effort */ }
}

function renderHealth(conn) {
  const st = $("#hStatus");
  st.textContent = conn.connected ? "Connected" : "Disconnected";
  st.className = "kv-value " + (conn.connected ? "on" : "off");
  $("#hUser").textContent = conn.user || conn.account_spec || "—";
  $("#hExpires").textContent = fmtDateTime(conn.token_expires);
  $("#hCheck").textContent = fmtDateTime(conn.last_check);
  $("#hRenew").textContent = fmtDateTime(conn.last_renew);
  const err = $("#hError");
  err.textContent = conn.last_error || "—";
  err.className = "kv-value " + (conn.last_error ? "off" : "");
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
  for (const [sym, t] of Object.entries(trades)) {
    const accts = t.accounts || {};
    const ids = Object.keys(accts);
    if (!ids.length) continue;
    for (const id of ids) {
      const a = accts[id];
      rows.push(`<tr><td>${sym}</td><td>${t.contract}</td>
        <td><span class="tag ${t.side}">${(t.side || "").toUpperCase()}</span></td>
        <td>${a.name || id}</td><td>${a.qty ?? "—"}</td>
        <td>${a.sl_order_id || "—"}</td>
        <td>${(a.tp_order_ids || []).join(", ") || "—"}</td></tr>`);
    }
  }
  tbody.innerHTML = rows.length ? rows.join("")
    : '<tr><td colspan="7" class="empty">None</td></tr>';
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
    loadAccounts();
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

/* --------------------------------------------------------------- accounts */
async function loadAccounts() {
  try { renderAccounts(await api("/api/accounts")); } catch (e) { /* ignore */ }
}

function renderAccounts(accounts) {
  const tbody = $("#accountsTable tbody");
  if (!accounts || !accounts.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty">No accounts — Connect or Refresh from Tradovate</td></tr>';
    return;
  }
  tbody.innerHTML = accounts.map((a) => `
    <tr data-id="${a.id}" data-name="${escapeHtml(a.name)}">
      <td><input type="checkbox" class="switch acc-enabled" ${a.enabled ? "checked" : ""} /></td>
      <td>${escapeHtml(a.name)}</td>
      <td>${a.id}</td>
      <td><input type="number" class="acc-mult" min="0.1" step="0.1" value="${a.qty_multiplier ?? 1}" style="width:80px" /></td>
    </tr>`).join("");
}

function collectAccounts() {
  return [...$$("#accountsTable tbody tr[data-id]")].map((tr) => ({
    id: Number(tr.dataset.id),
    name: tr.dataset.name,
    enabled: tr.querySelector(".acc-enabled").checked,
    qty_multiplier: Number(tr.querySelector(".acc-mult").value) || 1,
  }));
}

$("#refreshAccountsBtn").addEventListener("click", async () => {
  toast("Fetching accounts…");
  try {
    renderAccounts(await api("/api/accounts/refresh", { method: "POST" }));
    toast("Accounts refreshed", "success");
  } catch (e) { toast(e.message, "error"); }
});

$("#saveAccountsBtn").addEventListener("click", async () => {
  try {
    await api("/api/accounts", { method: "POST", body: JSON.stringify(collectAccounts()) });
    $("#accountsHint").textContent = "Saved ✓";
    setTimeout(() => ($("#accountsHint").textContent = ""), 2500);
    toast("Accounts saved", "success");
    refreshStatus();
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
  toast("Checking connection…");
  try {
    const conn = await api("/api/health", { method: "GET" });
    renderHealth(conn);
    toast(conn.connected ? "Connection healthy" : "Disconnected: " + (conn.last_error || ""),
      conn.connected ? "success" : "error");
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
  loadAccounts();
  loadScenarios();
  $("#testPayload").value = JSON.stringify(PRESETS.entry, null, 2);

  setInterval(refreshStatus, 5000);
  setInterval(refreshOrders, 7000);
  setInterval(refreshLogs, 8000);
}
boot();
