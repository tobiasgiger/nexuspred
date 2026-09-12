/* Fluxbridge dashboard — bootstrap, hash router, shell (sidebar + topbar),
   live stream and the reconcile polling. Build-free ES modules. */
import { $, h, clear, toast, closeDialogs } from "./ui.js";
import { store, can } from "./store.js";
import { actions } from "./actions.js";
import { connectStream } from "./stream.js";
import { parseHash, matchRoute, navigate as go } from "./router.js";
import { initTheme } from "./theme.js";
import { renderSidebar, titleFor } from "./components/sidebar.js";
import { renderTopbar } from "./components/topbar.js";
import { closeDrawer } from "./components/drawer.js";
import { ROUTES } from "./pages/index.js";
import { registerWorker } from "./push.js";
import { t } from "./i18n.js";

const shell = $("#shell");
const view = $("#view");
const sidebarEl = $("#sidebar");
const topbarEl = $("#topbar");
const VERSION = window.__FB_VERSION__ || "";

let collapsed = false;
try { collapsed = localStorage.getItem("np_sidebar_collapsed") === "1"; } catch (e) { /* ignore */ }

function setCollapsed(on) {
  collapsed = on;
  shell.classList.toggle("collapsed", on);
  try { localStorage.setItem("np_sidebar_collapsed", on ? "1" : "0"); } catch (e) { /* ignore */ }
  paintSidebar();
}

function navigate(path, opts = {}) {
  if (!opts.keepDrawer) shell.classList.remove("sidebar-open");
  go(path, opts);
}

function paintSidebar() {
  const r = store.get("route");
  renderSidebar(sidebarEl, { me: store.get("me"), path: r ? r.path : "/", collapsed, onToggleCollapse: setCollapsed, navigate, version: VERSION });
}

/* ------------------------------------------------------------- routing */
let cleanup = null;
let currentPath = null;

function render() {
  const { path, query } = parseHash();
  const me = store.get("me");
  const m = matchRoute(ROUTES, path);
  if (!m) { go("/", { replace: true }); return; }
  if (m.route.redirect) { go(m.route.redirect, { replace: true }); return; }
  const page = m.route.page;
  if (page.gate && !can(me, page.gate)) { toast(t("That page isn't available for your account"), "warn"); go("/", { replace: true }); return; }

  const samePage = currentPath !== null && matchRoute(ROUTES, currentPath)?.route.page === page;
  currentPath = path;
  if (samePage) {
    // Same page, different params (e.g. the webhook drawer deep link): the page
    // listens to `route` itself — don't tear it down.
    store.set("route", { path, params: m.params, query, title: titleFor(path) });
    paintSidebar();
    document.title = `${titleFor(path)} · Fluxbridge`;
    return;
  }
  // Tear the old page down first (it may close its drawer), then announce the route.
  closeDialogs();
  if (cleanup) { try { cleanup(); } catch (e) { console.error(e); } cleanup = null; }
  closeDrawer();
  store.set("route", { path, params: m.params, query, title: titleFor(path) });
  clear(view);
  view.scrollTop = 0;
  window.scrollTo({ top: 0 });
  try {
    cleanup = page.render(view, { params: m.params, query, navigate, store }) || null;
  } catch (e) {
    console.error(e);
    view.append(h("div", { class: "callout danger" }, t("This page failed to render: "), e.message));
  }
  paintSidebar();
  document.title = `${titleFor(path)} · Fluxbridge`;
}

/* ---------------------------------------------------------------- boot */
async function boot() {
  initTheme();
  shell.classList.toggle("collapsed", collapsed);
  $("#scrim").addEventListener("click", () => shell.classList.remove("sidebar-open"));
  renderTopbar(topbarEl, { navigate, onHamburger: () => shell.classList.toggle("sidebar-open"),
    onPrivacy: () => { currentPath = null; render(); } });   // repaint the page with (un)masked names

  try {
    await actions.loadMe();
  } catch (e) {
    view.append(h("div", { class: "callout danger" }, t("Could not load your account: "), e.message));
    return;
  }
  // Data the shell and most pages need right away.
  await Promise.all([actions.loadSettings().catch(() => null), actions.refreshStatus(), actions.loadWebhooks(), actions.loadTradeAccounts()]);
  window.addEventListener("hashchange", render);
  render();

  connectStream();
  registerWorker();  // push notifications (no-op where unsupported); never caches pages
  actions.refreshOrders();
  actions.refreshLogs();
  actions.refreshDiscordStatus();
  actions.loadDiscordFeed();
  if (can(store.get("me"), "admin") && (store.get("settings") || {}).auto_check_updates !== false) actions.checkUpdate();

  // Reconcile polling — the stream delivers changes instantly; these catch anything missed.
  setInterval(actions.refreshStatus, 15000);
  setInterval(actions.refreshOrders, 60000);
  setInterval(actions.refreshLogs, 60000);
  setInterval(actions.refreshDiscordStatus, 10000);
  let dirtyTimer = null;
  store.subscribe("statusDirty", () => { clearTimeout(dirtyTimer); dirtyTimer = setTimeout(actions.refreshStatus, 600); });
  store.subscribe("streamResync", () => { actions.refreshOrders(); actions.refreshLogs(); });   // after a stream gap: re-pull what we missed
  store.subscribe("me", paintSidebar);
}

boot();
