/* Small SVG chart kit (no dependencies — the CSP allows only self-hosted scripts).
   Marks follow the house rules: thin bars (≤24px, 4px rounded data-end, square
   at the baseline, 2px surface gap), 2px lines with a 10% area wash, hairline
   solid gridlines, text in text tokens (never the series colour), one tooltip
   for every mark, and a table twin next to every chart (built by the page). */
import { h, clear } from "./ui.js";

const NS = "http://www.w3.org/2000/svg";
export function svgEl(tag, attrs = {}, ...children) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null && v !== false) el.setAttribute(k, v);
  for (const c of children.flat()) if (c != null) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return el;
}

export const fmtMoney = (v, digits = 0) => {
  const n = Number(v) || 0;
  const s = Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return (n < 0 ? "−$" : "$") + s;
};
export const fmtSigned = (v, digits = 0) => (Number(v) > 0 ? "+" : "") + fmtMoney(v, digits);

/** Nice tick values for a numeric axis spanning [lo, hi] (lo ≤ 0 ≤ hi is typical). */
function ticks(lo, hi, n = 4) {
  if (lo === hi) { lo -= 1; hi += 1; }
  const span = hi - lo;
  const rough = span / n;
  const pow = Math.pow(10, Math.floor(Math.log10(rough)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * pow).find((s) => s >= rough) || pow * 10;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(Math.round(v * 1e6) / 1e6);
  return out;
}

/* ------------------------------------------------------------- tooltip */
let tip = null;
function tooltip() {
  if (!tip) { tip = h("div", { class: "viz-tip", hidden: true }); document.body.append(tip); }
  return tip;
}
export function showTip(x, y, rows) {
  const t = tooltip();
  clear(t);
  for (const r of rows) {
    const row = h("div", { class: "viz-tip-row" });
    if (r.swatch) row.append(h("span", { class: "viz-tip-key", style: `background:${r.swatch}` }));
    row.append(h("strong", null, r.value), h("span", { class: "viz-tip-label" }, r.label));
    t.append(row);
  }
  t.hidden = false;
  const w = t.offsetWidth, hh = t.offsetHeight;
  const left = Math.min(x + 14, window.innerWidth - w - 8);
  const top = Math.max(8, y - hh - 12);
  t.style.left = `${left}px`; t.style.top = `${top}px`;
}
export function hideTip() { if (tip) tip.hidden = true; }

function bindTip(el, rowsFn) {
  const move = (e) => showTip(e.clientX, e.clientY, rowsFn());
  el.addEventListener("pointermove", move);
  el.addEventListener("pointerenter", move);
  el.addEventListener("pointerleave", hideTip);
  el.setAttribute("tabindex", "0");
  el.addEventListener("focus", () => { const r = el.getBoundingClientRect(); showTip(r.left + r.width / 2, r.top, rowsFn()); });
  el.addEventListener("blur", hideTip);
}

/* ---------------------------------------------------------- responsive
   Charts are drawn in real pixels: `mount(container, build)` calls
   build(widthPx) now and again whenever the container resizes, so bars stay
   ≤24px and type stays at its true size instead of scaling with a viewBox. */
const observers = new WeakMap();
export function mount(container, build) {
  container._vizBuild = build;
  const render = () => {
    const w = Math.max(280, Math.floor(container.clientWidth || container.parentElement?.clientWidth || 600));
    if (container._vizW === w && container.firstChild) return;
    container._vizW = w;
    clear(container);
    container.append(container._vizBuild(w));
  };
  if (!observers.has(container) && typeof ResizeObserver !== "undefined") {
    const ro = new ResizeObserver(() => { container._vizW = null; render(); });
    ro.observe(container);
    observers.set(container, ro);
  }
  container._vizW = null;
  render();
}

/* ---------------------------------------------------------------- frame */
function frame({ width, height, pad }) {
  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width, height, class: "viz", role: "img" });
  const plot = { x: pad.l, y: pad.t, w: width - pad.l - pad.r, h: height - pad.t - pad.b };
  return { svg, plot };
}

function yAxis(svg, plot, lo, hi, fmt) {
  const tks = ticks(lo, hi);
  const g = svgEl("g", { class: "viz-axis" });
  for (const v of tks) {
    const y = plot.y + plot.h - ((v - lo) / (hi - lo)) * plot.h;
    g.append(svgEl("line", { x1: plot.x, x2: plot.x + plot.w, y1: y, y2: y, class: v === 0 ? "viz-baseline" : "viz-grid" }));
    g.append(svgEl("text", { x: plot.x - 6, y: y + 3.5, "text-anchor": "end", class: "viz-tick" }, fmt(v)));
  }
  svg.append(g);
  return (v) => plot.y + plot.h - ((v - lo) / (hi - lo)) * plot.h;
}

/* ----------------------------------------------------------- column chart
   data: [{label, value, sub?}] — a diverging (sign) colouring: positive =
   --viz-pos, negative = --viz-neg. Single measure → no legend. */
export function columnChart(data, { width = 760, height = 220, valueFmt = fmtSigned, maxLabels = 12, onSelect = null } = {}) {
  const n = data.length;
  const pad = { l: 56, r: 12, t: 16, b: 28 };
  const { svg, plot } = frame({ width, height, pad });
  if (!n) { svg.append(svgEl("text", { x: width / 2, y: height / 2, "text-anchor": "middle", class: "viz-empty" }, "No data")); return svg; }
  const vals = data.map((d) => Number(d.value) || 0);
  const lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
  const y = yAxis(svg, plot, lo, hi, (v) => fmtMoney(v));
  const band = plot.w / n;
  const bw = Math.min(24, Math.max(3, band - 2));
  const y0 = y(0);
  const best = vals.indexOf(Math.max(...vals)), worst = vals.indexOf(Math.min(...vals));
  const labelEvery = Math.max(1, Math.ceil(n / Math.min(maxLabels, Math.floor(plot.w / 44))));
  data.forEach((d, i) => {
    const v = vals[i];
    const x = plot.x + i * band + (band - bw) / 2;
    const top = Math.min(y(v), y0), hgt = Math.max(1, Math.abs(y(v) - y0));
    const r = Math.min(4, bw / 2, hgt);
    // rounded data-end only, square at the baseline
    const path = v >= 0
      ? `M${x},${y0} V${top + r} a${r},${r} 0 0 1 ${r},-${r} H${x + bw - r} a${r},${r} 0 0 1 ${r},${r} V${y0} Z`
      : `M${x},${y0} V${y0 + hgt - r} a${r},${r} 0 0 0 ${r},${r} H${x + bw - r} a${r},${r} 0 0 0 ${r},-${r} V${y0} Z`;
    const bar = svgEl("path", { d: path, class: `viz-bar ${v >= 0 ? "pos" : "neg"} ${v === 0 ? "zero" : ""}` });
    const hit = svgEl("rect", { x: plot.x + i * band, y: plot.y, width: band, height: plot.h, class: "viz-hit" });
    bindTip(hit, () => [{ value: valueFmt(v, 2), label: d.tipLabel || d.label }, ...(d.sub ? [{ value: d.sub, label: "" }] : [])]);
    hit.addEventListener("pointerenter", () => bar.classList.add("hover"));
    hit.addEventListener("pointerleave", () => bar.classList.remove("hover"));
    if (onSelect) { hit.style.cursor = "pointer"; hit.addEventListener("click", () => onSelect(d)); }
    svg.append(bar, hit);
    if (i % labelEvery === 0) svg.append(svgEl("text", { x: x + bw / 2, y: plot.y + plot.h + 16, "text-anchor": "middle", class: "viz-tick" }, d.label));
    if ((i === best && v > 0) || (i === worst && v < 0)) {   // label the extremes only
      // positives: above the cap; negatives: above the baseline (free space, no
      // collision with the x-axis band under the bar)
      svg.append(svgEl("text", { x: x + bw / 2, y: v >= 0 ? top - 4 : y0 - 4, "text-anchor": "middle", class: "viz-label" }, valueFmt(v)));
    }
  });
  return svg;
}

/* -------------------------------------------------------------- line chart
   points: [{x: label, value}] cumulative series, crosshair + tooltip. */
export function lineChart(points, { width = 760, height = 220, valueFmt = fmtSigned } = {}) {
  const n = points.length;
  const pad = { l: 56, r: 16, t: 12, b: 28 };
  const { svg, plot } = frame({ width, height, pad });
  if (!n) { svg.append(svgEl("text", { x: width / 2, y: height / 2, "text-anchor": "middle", class: "viz-empty" }, "No data")); return svg; }
  const vals = points.map((p) => Number(p.value) || 0);
  const lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
  const y = yAxis(svg, plot, lo, hi, (v) => fmtMoney(v));
  const x = (i) => plot.x + (n === 1 ? plot.w / 2 : (i / (n - 1)) * plot.w);
  const d = vals.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const y0 = y(0);
  svg.append(svgEl("path", { d: `${d} L${x(n - 1).toFixed(1)},${y0} L${x(0).toFixed(1)},${y0} Z`, class: "viz-area" }));
  svg.append(svgEl("path", { d, class: "viz-line" }));
  const last = vals[n - 1];
  svg.append(svgEl("circle", { cx: x(n - 1), cy: y(last), r: 4, class: "viz-dot" }));
  svg.append(svgEl("text", { x: x(n - 1) - 6, y: y(last) - 8, "text-anchor": "end", class: "viz-label" }, valueFmt(last)));
  const every = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(plot.w / 90))));
  points.forEach((p, i) => { if (i % every === 0 || i === n - 1) svg.append(svgEl("text", { x: x(i), y: plot.y + plot.h + 16, "text-anchor": i === n - 1 ? "end" : i === 0 ? "start" : "middle", class: "viz-tick" }, p.x)); });
  // crosshair layer
  const cross = svgEl("line", { x1: 0, x2: 0, y1: plot.y, y2: plot.y + plot.h, class: "viz-cross", visibility: "hidden" });
  const dot = svgEl("circle", { r: 4, class: "viz-dot", visibility: "hidden" });
  const hit = svgEl("rect", { x: plot.x, y: plot.y, width: plot.w, height: plot.h, class: "viz-hit" });
  svg.append(cross, dot, hit);
  const pick = (e) => {
    const r = svg.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * width;
    const i = Math.max(0, Math.min(n - 1, Math.round(((px - plot.x) / plot.w) * (n - 1))));
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(vals[i])); dot.setAttribute("visibility", "visible");
    showTip(e.clientX, e.clientY, [{ value: valueFmt(vals[i], 2), label: points[i].tip || points[i].x }]);
  };
  hit.addEventListener("pointermove", pick);
  hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); hideTip(); });
  return svg;
}

/* --------------------------------------------------------- horizontal bars
   data: [{label, value}] — single measure, sign-coloured. */
export function barList(data, { valueFmt = fmtSigned } = {}) {
  const max = Math.max(1, ...data.map((d) => Math.abs(Number(d.value) || 0)));
  const el = h("div", { class: "viz-bars" });
  for (const d of data) {
    const v = Number(d.value) || 0;
    const w = Math.round((Math.abs(v) / max) * 100);
    const row = h("div", { class: "viz-bar-row" },
      h("span", { class: "viz-bar-label" }, d.label),
      h("span", { class: "viz-bar-track" }, h("span", { class: `viz-bar-fill ${v >= 0 ? "pos" : "neg"}`, style: `width:${w}%` })),
      h("span", { class: `viz-bar-value ${v < 0 ? "neg" : ""}` }, valueFmt(v)));
    bindTip(row, () => [{ value: valueFmt(v, 2), label: d.label }, ...(d.sub ? [{ value: d.sub, label: "" }] : [])]);
    el.append(row);
  }
  if (!data.length) el.append(h("div", { class: "empty-state" }, "No data"));
  return el;
}

/* ------------------------------------------------------- calendar heat-map
   days: [{day: 'YYYY-MM-DD', weekday: 0..6 (Mon=0), net_pnl, trades}] for one month. */
/** A signed amount for a calendar cell: the full figure, plus a compact "+1.2k"
 *  form that CSS shows instead on narrow screens (phones ellipsized "+$1…"). */
function calVal(v) {
  const a = Math.abs(v), sign = v < 0 ? "−" : "+";
  const short = a >= 10000 ? `${sign}${Math.round(a / 1000)}k` : a >= 1000 ? `${sign}${(a / 1000).toFixed(1)}k` : `${sign}${Math.round(a)}`;
  return h("span", { class: "viz-cal-val" }, h("span", { class: "viz-cal-full" }, fmtSigned(v)), h("span", { class: "viz-cal-short" }, short));
}

export function calendarHeatmap(days, { onSelect = null } = {}) {
  const el = h("div", { class: "viz-cal" });
  for (const w of ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun", "Week"]) el.append(h("div", { class: "viz-cal-head" }, w));
  if (!days.length) return el;
  const max = Math.max(1, ...days.map((d) => Math.abs(Number(d.net_pnl) || 0)));
  for (let i = 0; i < days[0].weekday; i++) el.append(h("div", { class: "viz-cal-cell blank" }));
  let week = { net: 0, trades: 0, from: days[0].day };
  const weekCell = (last) => {
    // rightmost column: the week's total (Monday–Sunday within the shown month)
    const cell = h("div", { class: `viz-cal-cell week ${week.trades ? (week.net >= 0 ? "pos" : "neg") : "flat"}` },
      h("span", { class: "viz-cal-day" }, "Total"),
      week.trades ? calVal(week.net) : null);
    const net = week.net, trades = week.trades, from = week.from;
    bindTip(cell, () => [{ value: fmtSigned(net, 2), label: `week of ${from} – ${last}` }, { value: String(trades), label: trades === 1 ? "trade" : "trades" }]);
    el.append(cell);
  };
  for (const d of days) {
    const v = Number(d.net_pnl) || 0;
    const level = d.trades ? Math.max(1, Math.ceil((Math.abs(v) / max) * 4)) : 0;
    const cell = h("div", { class: `viz-cal-cell ${d.trades ? (v >= 0 ? "pos" : "neg") : "flat"} l${level}` },
      h("span", { class: "viz-cal-day" }, String(Number(d.day.slice(8)))),
      d.trades ? calVal(v) : null);
    bindTip(cell, () => [{ value: fmtSigned(v, 2), label: d.day }, { value: String(d.trades), label: d.trades === 1 ? "trade" : "trades" }]);
    if (onSelect && d.trades) { cell.style.cursor = "pointer"; cell.addEventListener("click", () => onSelect(d)); }
    el.append(cell);
    week.net += v; week.trades += Number(d.trades) || 0;
    if (d.weekday === 6) { weekCell(d.day); week = { net: 0, trades: 0, from: "" }; }
    else if (d === days[days.length - 1]) {
      for (let i = d.weekday + 1; i < 7; i++) el.append(h("div", { class: "viz-cal-cell blank" }));
      weekCell(d.day);
    } else if (!week.from) week.from = d.day;
  }
  return el;
}
