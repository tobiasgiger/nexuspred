import { t, locale } from "./i18n.js";
/* Tiny DOM + UI helpers: element builder, formatting, toasts, dialogs, clipboard. */

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const PROPS = new Set(["value", "checked", "disabled", "selected", "readOnly", "indeterminate", "open"]);

/** h("div", {class: "x", onClick: fn}, "text", node, [more]) → HTMLElement */
export function h(tag, attrs = null, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
      else if (k === "dataset") Object.assign(el.dataset, v);
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
      else if (PROPS.has(k)) el[k] = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export function replace(el, ...children) {
  clear(el);
  return append(el, children);
}


/* ------------------------------------------------------------ formatting */
export function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString(locale(), { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function fmtDateTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(locale(), { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function fmtNum(v, digits = 2) {
  if (v == null || v === "") return "—";
  const n = Number(v);
  return Number.isFinite(n) ? n.toLocaleString(locale(), { maximumFractionDigits: digits }) : String(v);
}


/* ---------------------------------------------------------------- toasts */
export function toast(message, type = "") {
  const root = $("#toasts");
  if (!root) return;
  const el = h("div", { class: `toast ${type}`, role: "status" }, message);
  root.append(el);
  requestAnimationFrame(() => el.classList.add("show"));
  setTimeout(() => {
    el.classList.remove("show");
    setTimeout(() => el.remove(), 300);
  }, type === "error" ? 5000 : 3400);
}

/* --------------------------------------------------------------- dialogs */
const _openDialogs = new Set();

/** Close every open confirm dialog (resolving it as cancelled) — e.g. on navigation. */
export function closeDialogs() {
  for (const finish of [..._openDialogs]) finish(false);
}

/** Accessible confirm dialog. Resolves true/false. */
export function confirmDialog({ title, body, confirmText = "Confirm", cancelText = "Cancel", danger = false }) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (done) return; done = true; _openDialogs.delete(finish); dlg.close(); dlg.remove(); resolve(v); };
    _openDialogs.add(finish);
    const ok = h("button", { class: `btn ${danger ? "btn-sos" : "btn-primary"}`, onClick: () => finish(true) }, confirmText);
    const dlg = h("dialog", { class: `dlg ${danger ? "danger" : ""}` },
      h("div", { class: "dlg-body" }, h("h2", null, title), body ? h("p", null, body) : null),
      h("div", { class: "dlg-actions" },
        h("button", { class: "btn btn-ghost", onClick: () => finish(false) }, cancelText),
        ok));
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); finish(false); });
    dlg.addEventListener("click", (e) => { if (e.target === dlg) finish(false); });
    document.body.append(dlg);
    dlg.showModal();
    (danger ? dlg.querySelector(".btn-ghost") : ok).focus();
  });
}

/* ------------------------------------------------------------- clipboard */
export async function copyText(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) { /* fall through to the legacy path (iOS Safari, broken gesture chain) */ }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch (e) {
    return false;
  }
}

export function copyButton(getText, label = "Copy", cls = "btn btn-ghost btn-sm") {
  return h("button", {
    type: "button", class: cls,
    onClick: async () => toast((await copyText(getText())) ? t("Copied") : t("Copy failed"), "success"),
  }, label);
}

/* ------------------------------------------------------------- utilities */
export function debounce(fn, ms) {
  let timer = null;
  const wrapped = (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
  wrapped.cancel = () => clearTimeout(timer);
  return wrapped;
}

/* Paint a prepend-only ring buffer (newest first) into `box`: when the new list
   is the old one with rows added on top, only those rows are inserted — a burst
   used to rebuild hundreds of DOM rows per frame. Identity-based: the store
   keeps the same objects for rows that did not change (see mergeLive). */
export function paintIncremental(box, prev, list, lineOf, emptyText) {
  if (prev && prev.length && list.length) {
    const n = list.indexOf(prev[0]);
    if (n >= 0 && n <= 50) {
      const overlap = Math.min(prev.length, list.length - n);
      let same = true;
      for (let i = 0; i < overlap; i++) if (list[n + i] !== prev[i]) { same = false; break; }
      if (same) {
        if (n === 0 && list.length === prev.length) return;
        const empty = box.querySelector(".empty-state");
        if (empty) empty.remove();
        for (let i = n - 1; i >= 0; i--) box.prepend(lineOf(list[i]));
        while (box.childElementCount > list.length) box.lastElementChild.remove();
        return;
      }
    }
  }
  clear(box);
  if (!list.length) box.append(h("div", { class: "empty-state" }, emptyText));
  else box.append(...list.map(lineOf));
}

/* Merge a freshly fetched ring buffer with the live one: rows the stream
   delivered while the fetch was in flight stay on top, and rows that are the
   same as before keep their object identity (so incremental painters do not
   rebuild). Rows are matched by `keyOf` (default: JSON of the row). */
export function mergeLive(prev, fetched, keyOf = (x) => JSON.stringify(x), cap = 500) {
  const before = new Map();
  for (const x of prev || []) before.set(keyOf(x), x);
  const seen = new Set();
  const out = [];
  for (const x of fetched || []) {
    const k = keyOf(x);
    seen.add(k);
    out.push(before.get(k) || x);
  }
  // live rows that arrived after the fetch started sit above the fetched top row
  const fresh = [];
  for (const x of prev || []) {
    const k = keyOf(x);
    if (seen.has(k)) break;                 // reached the first row the fetch already knows: the rest is older
    fresh.push(x);
  }
  return fresh.concat(out).slice(0, cap);
}

export function tag(text, tone = "") {
  return h("span", { class: `tag ${tone}` }, text);
}

export function empty(message, icon = null) {
  return h("div", { class: "empty-state" }, icon, h("div", null, message));
}

/** Card with optional header actions. */
export function card({ title, actions = [], hint = null, cls = "" }, ...body) {
  return h("div", { class: `card ${cls}` },
    title ? h("div", { class: "card-head" }, h("h2", null, title), actions.length ? h("div", { class: "actions" }, actions) : null) : null,
    hint ? h("p", { class: "hint" }, hint) : null,
    body);
}

export function pageHead(title, lead, actions = []) {
  return h("div", { class: "page-head" },
    h("div", null, h("h1", null, title), lead ? h("p", { class: "lead" }, lead) : null),
    actions.length ? h("div", { class: "actions" }, actions) : null);
}
