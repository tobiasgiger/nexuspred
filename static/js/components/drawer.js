/* Right-hand drawer (detail panel) with scrim, Esc to close. One at a time. */
import { h, $, clear } from "../ui.js";
import { icon } from "../icons.js";

let current = null;

export function openDrawer({ title, body, foot = null, onClose = null, width = null }) {
  closeDrawer();
  const root = $("#drawerRoot");
  root.className = "drawer-root";
  const titleEl = h("h2", null, title);
  const bodyEl = h("div", { class: "drawer-body" }, body);
  const footEl = h("div", { class: "drawer-foot" }, foot);
  const panel = h("div", { class: "drawer", role: "dialog", "aria-modal": "true", style: width ? `width:min(${width}, 100vw)` : null },
    h("div", { class: "drawer-head" },
      titleEl,
      h("button", { type: "button", class: "btn btn-ghost btn-icon", title: "Close", onClick: () => closeDrawer() }, icon("x"))),
    bodyEl, foot ? footEl : null);
  const scrim = h("div", { class: "drawer-scrim", onClick: () => closeDrawer() });
  root.append(scrim, panel);
  requestAnimationFrame(() => root.classList.add("open"));
  const onKey = (e) => { if (e.key === "Escape") closeDrawer(); };
  document.addEventListener("keydown", onKey);
  document.body.style.overflow = "hidden";
  current = { root, onClose, onKey, titleEl, bodyEl, footEl };
  return {
    setTitle: (t) => { titleEl.textContent = t; },
    setBody: (...c) => { clear(bodyEl); bodyEl.append(...c); },
    setFoot: (...c) => { clear(footEl); footEl.append(...c); if (!footEl.parentNode) panel.append(footEl); },
    close: closeDrawer,
  };
}

export function closeDrawer() {
  if (!current) return;
  const { root, onClose, onKey } = current;
  current = null;
  document.removeEventListener("keydown", onKey);
  document.body.style.overflow = "";
  root.classList.remove("open");
  setTimeout(() => { clear(root); root.className = ""; }, 220);
  if (onClose) onClose();
}

export function drawerOpen() {
  return !!current;
}
