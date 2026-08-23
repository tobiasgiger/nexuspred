"use strict";

// These two functions are injected into the Discord tab via chrome.scripting.
// They must be fully self-contained (no references to popup scope), because
// executeScript serialises them and runs them in the page's context.

// Runs in the extension's ISOLATED world (default). Discord deletes its own
// window.localStorage reference in the page's MAIN world, but the isolated world
// still sees the same origin's localStorage — so this is the reliable path.
function grabTokenIsolated() {
  const isTokenLike = (v) =>
    /^[\w-]{20,}\.[\w-]{5,}\.[\w-]{20,}$/.test(v) || /^mfa\.[\w-]{20,}$/.test(v);
  const unquote = (v) => (typeof v === "string" ? v.replace(/^"+|"+$/g, "") : v);
  try {
    const direct = window.localStorage && window.localStorage.getItem("token");
    if (direct) {
      const v = unquote(direct);
      if (isTokenLike(v)) return { token: v, method: "localStorage.token" };
    }
  } catch (e) { /* localStorage may be blocked; fall through */ }
  try {
    for (let i = 0; i < window.localStorage.length; i++) {
      const key = window.localStorage.key(i);
      const v = unquote(window.localStorage.getItem(key));
      if (typeof v === "string" && isTokenLike(v)) {
        return { token: v, method: "localStorage[" + key + "]" };
      }
    }
  } catch (e) { /* ignore */ }
  return { token: null, method: null };
}

// Fallback that runs in the page's MAIN world and pulls the token out of
// Discord's webpack modules. More fragile (breaks when Discord reshuffles
// internals) but useful if localStorage is unavailable.
function grabTokenMain() {
  try {
    const chunk = window.webpackChunkdiscord_app;
    if (!chunk) return { token: null, method: null };
    let modules;
    chunk.push([
      [Symbol("nexuspred")],
      {},
      (req) => { modules = Object.values(req.c); },
    ]);
    chunk.pop();
    if (!modules) return { token: null, method: null };
    for (const m of modules) {
      try {
        const exp = m && m.exports;
        if (!exp) continue;
        const getToken =
          (exp.default && exp.default.getToken) ||
          exp.getToken ||
          (exp.Z && exp.Z.getToken) ||
          (exp.ZP && exp.ZP.getToken);
        if (typeof getToken === "function") {
          const t = getToken();
          if (t) return { token: t, method: "webpack" };
        }
      } catch (e) { /* keep scanning */ }
    }
  } catch (e) { /* ignore */ }
  return { token: null, method: null };
}

const $ = (id) => document.getElementById(id);
let currentToken = null;
let revealed = false;

function setStatus(msg, cls) {
  const el = $("status");
  el.textContent = msg || "";
  el.className = "status" + (cls ? " " + cls : "");
}

function render() {
  const ta = $("token");
  if (!currentToken) {
    ta.value = "";
    $("copyBtn").disabled = true;
    $("revealBtn").disabled = true;
    return;
  }
  ta.value = revealed
    ? currentToken
    : currentToken.slice(0, 6) + "…" + "•".repeat(18) + "…" + currentToken.slice(-4);
  $("copyBtn").disabled = false;
  $("revealBtn").disabled = false;
  $("revealBtn").textContent = revealed ? "Hide" : "Reveal";
}

async function runInTab(tabId, func, world) {
  const [res] = await chrome.scripting.executeScript({
    target: { tabId },
    func,
    world, // "ISOLATED" (default) or "MAIN"
  });
  return (res && res.result) || { token: null, method: null };
}

async function getToken() {
  setStatus("Reading…", "");
  currentToken = null;
  $("method").textContent = "";
  render();

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.url || !/^https?:\/\/([a-z0-9-]+\.)?discord\.com\//i.test(tab.url)) {
    setStatus("Open discord.com (logged in) in the active tab first.", "err");
    return;
  }

  try {
    let out = await runInTab(tab.id, grabTokenIsolated, "ISOLATED");
    if (!out.token) out = await runInTab(tab.id, grabTokenMain, "MAIN");

    if (out.token) {
      currentToken = out.token;
      revealed = false;
      render();
      $("method").textContent = "source: " + out.method;
      setStatus("Token found. Copy it, then paste into the bridge.", "ok");
    } else {
      setStatus("No token found. Make sure you're logged in to Discord in this tab.", "err");
    }
  } catch (e) {
    setStatus("Couldn't read the tab: " + (e && e.message ? e.message : e), "err");
  }
}

$("getBtn").addEventListener("click", getToken);

$("copyBtn").addEventListener("click", async () => {
  if (!currentToken) return;
  try {
    await navigator.clipboard.writeText(currentToken);
    setStatus("Copied to clipboard. Treat it like a password.", "ok");
  } catch (e) {
    setStatus("Copy failed — reveal and copy manually.", "err");
  }
});

$("revealBtn").addEventListener("click", () => {
  revealed = !revealed;
  render();
});

// Auto-run on open for convenience.
getToken();
