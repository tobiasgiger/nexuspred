/* Data table: columns describe how to render each cell; update(rows) re-renders. */
import { h, clear } from "../ui.js";

/**
 * dataTable({
 *   columns: [{ label, render(row) → node|string, className }],
 *   empty: "message", onRow(row, tr), rowClass(row), compact
 * }) → { el, update(rows), tbody }
 */
export function dataTable({ columns, empty = "Nothing here yet", onRow = null, rowClass = null, compact = false }) {
  const tbody = h("tbody");
  const table = h("table", { class: `data-table ${compact ? "compact" : ""}` },
    h("thead", null, h("tr", null, columns.map((c) => h("th", { class: c.className || null }, c.label)))),
    tbody);
  const el = h("div", { class: "table-scroll" }, table);
  let lastKey = null;

  function update(rows, { force = false } = {}) {
    // pollers hand the same rows back every few seconds: a cheap fingerprint
    // spares the DOM the full clear-and-rebuild (and keeps scroll/selection)
    let key = null;
    if (!force) {
      try { key = JSON.stringify(rows || []); } catch { key = null; }
      if (key !== null && key === lastKey) return;
    }
    lastKey = key;
    clear(tbody);
    if (!rows || !rows.length) {
      tbody.append(h("tr", null, h("td", { class: "empty", colspan: columns.length }, empty)));
      return;
    }
    for (const row of rows) {
      const tr = h("tr", { class: `${rowClass ? rowClass(row) || "" : ""} ${onRow ? "clickable" : ""}`.trim() || null },
        columns.map((c) => h("td", { class: c.className || null }, c.render(row))));
      if (onRow) {
        tr.addEventListener("click", (e) => {
          if (e.target.closest("input,button,select,textarea,a,label,.switch")) return;
          onRow(row, tr);
        });
      }
      tbody.append(tr);
    }
  }

  return { el, update, tbody, table };
}
