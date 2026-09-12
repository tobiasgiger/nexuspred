/* Sidebar navigation: groups, a collapsible Settings section, icon rail, mobile drawer. */
import { h, clear } from "../ui.js";
import { icon } from "../icons.js";
import { can } from "../store.js";

export const NAV = [
  { group: "Monitoring", items: [
    { path: "/", label: "Overview", icon: "dashboard" },
    { path: "/discord", label: "Discord", icon: "discord", gate: "discord" },
    { path: "/logs", label: "Logs", icon: "logs" },
    { path: "/journal", label: "Journal", icon: "activity" },
    { path: "/calendar", label: "Calendar", icon: "calendar" },
  ] },
  { group: "Routing", items: [
    { path: "/webhooks", label: "Webhooks", icon: "webhook" },
    { path: "/marketplace", label: "Marketplace", icon: "store" },
    { path: "/copy", label: "Copy Trading", icon: "share" },
  ] },
  { group: "Configuration", items: [
    { path: "/settings", label: "Settings", icon: "settings", children: [
      { path: "/settings/general", label: "General & Trading" },
      { path: "/settings/accounts", label: "Broker Accounts" },
      { path: "/settings/symbols", label: "Symbol Mapping" },
      { path: "/settings/discord", label: "Discord Listener", gate: "discord" },
      { path: "/settings/alerts", label: "Alerts" },
      { path: "/settings/security", label: "Security" },
      { path: "/settings/account", label: "Account" },
      { path: "/settings/users", label: "Users", gate: "admin" },
      { path: "/settings/agents", label: "Execution Agents", gate: "admin" },
      { path: "/settings/news", label: "News & Calendar", gate: "admin" },
      { path: "/settings/updates", label: "Updates", gate: "admin" },
    ] },
  ] },
  { group: "Tools", items: [
    { path: "/tools", label: "Tools", icon: "tools" },
    { path: "/simulator", label: "Simulator", icon: "flask" },
  ] },
  { group: "Help", items: [
    { path: "/guide", label: "Setup Guide", icon: "book" },
  ] },
];

const visible = (me, item) => !item.gate || can(me, item.gate);

export function firstChild(me, item) {
  return (item.children || []).find((c) => visible(me, c));
}

/** Title for the topbar from the current path. */
export function titleFor(path) {
  for (const g of NAV) for (const it of g.items) {
    if (it.path === path) return it.label;
    for (const c of it.children || []) if (c.path === path) return `${it.label} · ${c.label}`;
  }
  if (path.startsWith("/webhooks/")) return "Webhooks";
  return "Fluxbridge";
}

export function renderSidebar(root, { me, path, collapsed, onToggleCollapse, navigate, version }) {
  clear(root);
  root.append(h("div", { class: "brand" },
    h("span", { class: "logo", "aria-hidden": "true" }, "◈"),
    h("div", { class: "brand-text" }, h("strong", null, "Fluxbridge"), h("span", null, "TradingView → your broker"))));

  const nav = h("nav", { class: "nav" });
  for (const g of NAV) {
    const items = g.items.filter((it) => visible(me, it));
    if (!items.length) continue;
    nav.append(h("div", { class: "nav-group" }, g.group));
    for (const it of items) {
      if (it.children) {
        const kids = it.children.filter((c) => visible(me, c));
        const activeChild = kids.some((c) => c.path === path);
        const childrenEl = h("div", { class: `nav-children ${activeChild ? "open" : ""}` },
          kids.map((c) => h("a", { class: `nav-item sub ${c.path === path ? "active" : ""}`, href: "#" + c.path,
            onClick: (e) => { e.preventDefault(); navigate(c.path); } }, h("span", { class: "lbl" }, c.label))));
        const parent = h("button", { type: "button", class: `nav-item ${activeChild ? "active expanded" : ""}`, title: it.label,
          onClick: () => {
            if (collapsed) { onToggleCollapse(false); }
            const open = childrenEl.classList.toggle("open");
            parent.classList.toggle("expanded", open);
            if (open && !activeChild) { const f = firstChild(me, it); if (f) navigate(f.path, { keepDrawer: true }); }
          } },
          icon(it.icon), h("span", { class: "lbl" }, it.label), icon("chevron", "caret"));
        nav.append(parent, childrenEl);
      } else {
        nav.append(h("a", { class: `nav-item ${it.path === path ? "active" : ""}`, href: "#" + it.path, title: it.label,
          onClick: (e) => { e.preventDefault(); navigate(it.path); } },
          icon(it.icon), h("span", { class: "lbl" }, it.label)));
      }
    }
  }
  root.append(nav);
  root.append(h("div", { class: "sidebar-foot" },
    h("button", { type: "button", class: "sidebar-collapse", title: collapsed ? "Expand sidebar" : "Collapse sidebar",
      onClick: () => onToggleCollapse(!collapsed) },
      icon("chevronLeft"), h("span", { class: "lbl" }, `Collapse · v${version}`))));
}
