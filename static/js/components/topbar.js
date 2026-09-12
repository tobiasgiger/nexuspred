/* Top bar: page title, stream/connection/trading pills, SOS, update badge,
   theme toggle and the user menu. Everything is live via the store. */
import { h, clear, toast, confirmDialog } from "../ui.js";
import { icon } from "../icons.js";
import { store } from "../store.js";
import { actions } from "../actions.js";
import { getTheme, setTheme } from "../theme.js";
import { isPrivate, setPrivate } from "../privacy.js";

export function renderTopbar(root, { navigate, onHamburger, onPrivacy }) {
  clear(root);
  const title = h("span", { class: "title" }, "Fluxbridge");

  // Stream liveness
  const streamDot = h("span", { class: "dot off" });
  const streamPill = h("span", { class: "pill hide-mobile", title: "Live updates (Server-Sent Events)" }, streamDot, h("span", { class: "pill-text" }, "offline"));

  // Broker connection
  const connDot = h("span", { class: "dot" });
  const connText = h("span", { class: "pill-text" }, "Disconnected");
  const connPill = h("button", { type: "button", class: "pill clickable", title: "Connection health — open Overview", onClick: () => navigate("/") }, connDot, connText);

  // Trading kill-switch
  const tradingText = h("strong", null, "—");
  const tradingPill = h("button", { type: "button", class: "pill clickable", title: "Toggle the trading master switch" },
    icon("zap", "ic"), h("span", { class: "pill-text" }, "Trading "), tradingText);
  tradingPill.addEventListener("click", async () => {
    const cur = !!(store.get("settings") || {}).trading_enabled;
    const ok = await confirmDialog({
      title: cur ? "Disable trading?" : "Enable trading?",
      body: cur ? "Incoming signals will be logged but NOT executed until you enable trading again."
        : "Incoming signals on enabled webhooks will place REAL orders on the routed accounts.",
      confirmText: cur ? "Disable trading" : "Enable trading", danger: !cur,
    });
    if (!ok) return;
    try { await actions.setTrading(!cur); } catch (e) { toast(e.message, "error"); }
  });

  // News lock (economic calendar): shown only while a window is active
  const newsPill = h("button", { type: "button", class: "pill clickable off hidden", title: "News lock active — no new entries; open Settings → News & Calendar", onClick: () => navigate("/settings/news") },
    icon("alert", "ic"), h("span", { class: "pill-text" }, "News lock"));

  // SOS
  const sosBtn = h("button", { type: "button", class: "btn btn-sos btn-sm", title: "Flatten ALL accounts now" }, "🆘", h("span", { class: "pill-text" }, "Flatten all"));
  sosBtn.addEventListener("click", async () => {
    const ok = await confirmDialog({
      title: "🆘 Flatten ALL accounts",
      body: "Cancel every working order and close every open position on ALL of your Tradovate accounts, right now — even if trading is paused.\n\nThis cannot be undone.",
      confirmText: "Flatten everything", danger: true,
    });
    if (!ok) return;
    sosBtn.disabled = true;
    try {
      const r = await actions.flattenAll();
      const msg = `Flattened ${r.flattened} position(s), cancelled ${r.cancelled} order(s) on ${r.accounts} account(s)`;
      if (r.errors && r.errors.length) toast(`${msg} — ${r.errors.length} error(s), see Logs`, "error");
      else toast(msg, "success");
    } catch (e) {
      toast("Flatten all failed: " + e.message, "error");
    } finally {
      sosBtn.disabled = false;
    }
  });

  // Update badge
  const updateBtn = h("button", { type: "button", class: "btn btn-update btn-sm hidden", onClick: () => navigate("/settings/updates") }, "Update available");

  // Theme toggle
  const themeBtn = h("button", { type: "button", class: "btn btn-ghost btn-icon hide-mobile", title: "Toggle dark / light" });
  const themeMenuItem = h("button", { type: "button" }, icon("moon"), "Switch theme");
  const paintTheme = () => {
    const dark = getTheme() === "dark";
    themeBtn.replaceChildren(icon(dark ? "sun" : "moon"));
    themeMenuItem.replaceChildren(icon(dark ? "sun" : "moon"), dark ? "Light theme" : "Dark theme");
  };
  const flipTheme = () => { setTheme(getTheme() === "dark" ? "light" : "dark"); paintTheme(); };
  themeBtn.addEventListener("click", flipTheme);
  themeMenuItem.addEventListener("click", flipTheme);
  paintTheme();

  // Privacy mode (mask account names for screenshots / streaming)
  const privacyBtn = h("button", { type: "button", class: "btn btn-ghost btn-icon hide-mobile" });
  const privacyMenuItem = h("button", { type: "button" });
  const paintPrivacy = () => {
    const on = isPrivate();
    privacyBtn.title = on ? "Privacy mode on — account names are masked. Click to show them." : "Privacy mode — mask account names (for screenshots / streaming)";
    privacyBtn.classList.toggle("active", on);
    privacyBtn.replaceChildren(icon(on ? "eyeOff" : "eye"));
    privacyMenuItem.replaceChildren(icon(on ? "eye" : "eyeOff"), on ? "Show account names" : "Mask account names");
  };
  const flipPrivacy = () => { setPrivate(!isPrivate()); paintPrivacy(); if (onPrivacy) onPrivacy(isPrivate()); };
  privacyBtn.addEventListener("click", flipPrivacy);
  privacyMenuItem.addEventListener("click", flipPrivacy);
  paintPrivacy();

  // User menu
  const who = h("div", { class: "who" }, h("strong", null, "…"), "signed in");
  const avatar = h("button", { type: "button", class: "avatar", title: "Account" }, "?");
  const menu = h("div", { class: "menu" }, who,
    h("a", { href: "#/settings/account", onClick: (e) => { e.preventDefault(); userMenu.classList.remove("open"); navigate("/settings/account"); } }, icon("user"), "Account"),
    themeMenuItem,
    privacyMenuItem,
    h("a", { href: "/logout", onClick: async (e) => {
      // signing out is a POST: a plain link (or a cross-site <img>) must never end a session
      e.preventDefault();
      try { await fetch("/logout", { method: "POST", credentials: "same-origin", redirect: "manual" }); } catch { /* cookie is cleared server-side; fall through */ }
      window.location.href = "/login";
    } }, icon("logout"), "Sign out"));
  const userMenu = h("div", { class: "user-menu" }, avatar, menu);
  avatar.addEventListener("click", (e) => { e.stopPropagation(); userMenu.classList.toggle("open"); });
  document.addEventListener("click", () => userMenu.classList.remove("open"));

  root.append(
    h("button", { type: "button", class: "btn btn-ghost btn-icon hamburger", title: "Menu", onClick: onHamburger }, icon("menu")),
    title, h("span", { class: "spacer" }),
    h("div", { class: "right" }, updateBtn, streamPill, connPill, tradingPill, newsPill, sosBtn, privacyBtn, themeBtn, userMenu));

  const unsubs = [
    store.subscribe("route", (r) => { title.textContent = r ? r.title : "Fluxbridge"; }, { immediate: true }),
    store.subscribe("stream", (s) => {
      streamDot.className = "dot " + (s === "live" ? "on" : s === "reconnecting" ? "warn" : "off");
      streamPill.querySelector(".pill-text").textContent = s === "live" ? "live" : s === "reconnecting" ? "reconnecting…" : "offline";
    }, { immediate: true }),
    store.subscribe("status", (s) => {
      const c = (s && s.connection) || {};
      const total = c.accounts_total || 0, con = c.accounts_connected || 0;
      connDot.className = "dot" + (c.connected ? " on" : total ? "" : " off");
      connText.textContent = total ? `${con}/${total} connected` : "No logins";
      connPill.className = "pill clickable " + (c.connected ? "on" : total ? "off" : "");
      const nl = s && s.news_lock;
      newsPill.classList.toggle("hidden", !(nl && nl.active));
      if (nl && nl.active) newsPill.title = `News lock: ${nl.active.title} — no new entries until ${new Date(nl.active.lock_until).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
    }, { immediate: true }),
    store.subscribe("settings", (st) => {
      const on = !!(st && st.trading_enabled);
      tradingText.textContent = on ? "ON" : "OFF";
      tradingPill.className = "pill clickable " + (on ? "on" : "off");
    }, { immediate: true }),
    store.subscribe("update", (u) => updateBtn.classList.toggle("hidden", !(u && u.update_available && store.get("me")?.is_admin)), { immediate: true }),
    store.subscribe("me", (me) => {
      if (!me) return;
      avatar.textContent = (me.email || "?").slice(0, 2).toUpperCase();
      who.replaceChildren(h("strong", null, me.email), me.is_admin ? "Administrator" : "User");
    }, { immediate: true }),
  ];
  return () => unsubs.forEach((u) => u());
}
