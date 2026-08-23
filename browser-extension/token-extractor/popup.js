"use strict";

/* ============================================================================
 * Injected extractors. Each is serialised by chrome.scripting.executeScript and
 * runs in the target tab, so they must be fully self-contained (no references to
 * popup scope). All are read-only and return plain data.
 * ==========================================================================*/

// --- Discord: ISOLATED world (default). Discord deletes its own
// window.localStorage in the page's MAIN world, but the isolated world still
// sees the same origin's localStorage — the reliable path.
function grabDiscordIsolated() {
  const isTokenLike = (v) =>
    /^[\w-]{20,}\.[\w-]{5,}\.[\w-]{20,}$/.test(v) || /^mfa\.[\w-]{20,}$/.test(v);
  const unquote = (v) => (typeof v === "string" ? v.replace(/^"+|"+$/g, "") : v);
  try {
    const direct = window.localStorage && window.localStorage.getItem("token");
    if (direct) {
      const v = unquote(direct);
      if (isTokenLike(v)) return [{ label: "Discord user token", value: v, source: "localStorage.token" }];
    }
  } catch (e) { /* fall through */ }
  try {
    for (let i = 0; i < window.localStorage.length; i++) {
      const key = window.localStorage.key(i);
      const v = unquote(window.localStorage.getItem(key));
      if (typeof v === "string" && isTokenLike(v)) {
        return [{ label: "Discord user token", value: v, source: "localStorage[" + key + "]" }];
      }
    }
  } catch (e) { /* ignore */ }
  return [];
}

// --- Discord: MAIN world webpack fallback (fragile; used only if isolated fails)
function grabDiscordMain() {
  try {
    const chunk = window.webpackChunkdiscord_app;
    if (!chunk) return [];
    let modules;
    chunk.push([[Symbol("nexuspred")], {}, (req) => { modules = Object.values(req.c); }]);
    chunk.pop();
    if (!modules) return [];
    for (const m of modules) {
      try {
        const exp = m && m.exports;
        if (!exp) continue;
        const getToken =
          (exp.default && exp.default.getToken) || exp.getToken ||
          (exp.Z && exp.Z.getToken) || (exp.ZP && exp.ZP.getToken);
        if (typeof getToken === "function") {
          const t = getToken();
          if (t) return [{ label: "Discord user token", value: t, source: "webpack" }];
        }
      } catch (e) { /* keep scanning */ }
    }
  } catch (e) { /* ignore */ }
  return [];
}

// --- Tradovate: reads the session token + checkToken (and any other token-ish
// keys) from local/session storage, so it still surfaces them if Tradovate
// renames keys.
function grabTradovate() {
  const unquote = (v) => (typeof v === "string" ? v.replace(/^"+|"+$/g, "") : v);
  const preferred = [
    "token", "checkToken",
    "access_token", "accessToken", "mdAccessToken", "md_token", "mdToken",
  ];
  const labelFor = {
    token: "Tradovate token", checkToken: "Tradovate checkToken",
  };
  const stores = [];
  try { if (window.localStorage) stores.push(["localStorage", window.localStorage]); } catch (e) {}
  try { if (window.sessionStorage) stores.push(["sessionStorage", window.sessionStorage]); } catch (e) {}

  const out = [];
  const seen = new Set();
  const add = (name, storeName, value) => {
    const key = name + "@" + storeName;
    if (!value || seen.has(key)) return;
    seen.add(key);
    out.push({ label: labelFor[name] || name, value: unquote(value), source: storeName });
  };

  for (const [storeName, store] of stores) {
    for (const name of preferred) {
      try { add(name, storeName, store.getItem(name)); } catch (e) {}
    }
  }
  // Catch anything else with "token" in the key name.
  for (const [storeName, store] of stores) {
    try {
      for (let i = 0; i < store.length; i++) {
        const k = store.key(i);
        if (/token/i.test(k)) add(k, storeName, store.getItem(k));
      }
    } catch (e) {}
  }
  // Sort so token + checkToken come first.
  out.sort((a, b) => {
    const rank = (l) => (l.startsWith("Tradovate token") ? 0 : l.startsWith("Tradovate checkToken") ? 1 : 2);
    return rank(a.label) - rank(b.label);
  });
  return out;
}

/* ============================================================================
 * Popup UI
 * ==========================================================================*/
const $ = (id) => document.getElementById(id);

function setStatus(msg, cls) {
  const el = $("status");
  el.textContent = msg || "";
  el.className = "status" + (cls ? " " + cls : "");
}

function mask(v) {
  if (!v || v.length <= 14) return "•".repeat((v && v.length) || 6);
  return v.slice(0, 6) + "…" + "•".repeat(14) + "…" + v.slice(-4);
}

function renderEntries(entries) {
  const box = $("results");
  box.innerHTML = "";
  for (const e of entries) {
    const wrap = document.createElement("div");
    wrap.className = "entry";

    const label = document.createElement("div");
    label.className = "label";
    label.textContent = e.label;
    const src = document.createElement("span");
    src.className = "src";
    src.textContent = e.source ? "(" + e.source + ")" : "";
    label.appendChild(src);

    const ta = document.createElement("textarea");
    ta.className = "val";
    ta.rows = 2;
    ta.readOnly = true;
    ta.value = mask(e.value);

    const acts = document.createElement("div");
    acts.className = "acts";
    const copyBtn = document.createElement("button");
    copyBtn.className = "ghost";
    copyBtn.textContent = "Copy";
    const revealBtn = document.createElement("button");
    revealBtn.className = "ghost";
    revealBtn.textContent = "Reveal";

    let revealed = false;
    revealBtn.addEventListener("click", () => {
      revealed = !revealed;
      ta.value = revealed ? e.value : mask(e.value);
      revealBtn.textContent = revealed ? "Hide" : "Reveal";
    });
    copyBtn.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(e.value);
        setStatus("Copied “" + e.label + "”. Treat it like a password.", "ok");
      } catch (err) {
        setStatus("Copy failed — reveal and copy manually.", "err");
      }
    });

    acts.appendChild(copyBtn);
    acts.appendChild(revealBtn);
    wrap.appendChild(label);
    wrap.appendChild(ta);
    wrap.appendChild(acts);
    box.appendChild(wrap);
  }
}

async function runInTab(tabId, func, world) {
  const [res] = await chrome.scripting.executeScript({ target: { tabId }, func, world });
  return (res && res.result) || [];
}

function hostOf(url) {
  try { return new URL(url).hostname; } catch (e) { return ""; }
}

async function extract(force) {
  setStatus("Reading…", "");
  $("results").innerHTML = "";

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.url) { setStatus("No active tab.", "err"); return; }
  const host = hostOf(tab.url);
  const isDiscord = /(^|\.)discord\.com$/i.test(host);
  const isTradovate = /(^|\.)tradovate\.com$/i.test(host);

  let service = force;
  if (!service) service = isDiscord ? "discord" : isTradovate ? "tradovate" : null;

  if (!service) {
    setStatus("Open a discord.com or tradovate.com tab (logged in), then retry.", "err");
    return;
  }
  if (service === "discord" && !isDiscord) {
    setStatus("Active tab isn't discord.com — open Discord and retry.", "err");
    return;
  }
  if (service === "tradovate" && !isTradovate) {
    setStatus("Active tab isn't tradovate.com — open the Tradovate web trader and retry.", "err");
    return;
  }

  try {
    let entries = [];
    if (service === "discord") {
      entries = await runInTab(tab.id, grabDiscordIsolated, "ISOLATED");
      if (!entries.length) entries = await runInTab(tab.id, grabDiscordMain, "MAIN");
    } else {
      entries = await runInTab(tab.id, grabTradovate, "ISOLATED");
    }

    if (entries.length) {
      renderEntries(entries);
      const svc = service === "discord" ? "Discord" : "Tradovate";
      setStatus(svc + ": " + entries.length + " token(s) found. Copy, then paste into the bridge.", "ok");
    } else {
      setStatus(
        service === "discord"
          ? "No Discord token found — make sure you're logged in in this tab."
          : "No Tradovate token found — log in to the web trader and retry.",
        "err"
      );
    }
  } catch (e) {
    setStatus("Couldn't read the tab: " + (e && e.message ? e.message : e), "err");
  }
}

$("autoBtn").addEventListener("click", () => extract(null));
$("discordBtn").addEventListener("click", () => extract("discord"));
$("tradovateBtn").addEventListener("click", () => extract("tradovate"));

// Auto-detect and run on open for convenience.
extract(null);
