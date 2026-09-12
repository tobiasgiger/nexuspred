/* Shared account pieces: the routed-accounts table used by the webhook drawer
   and the marketplace subscription drawer, and the account key both use. */
import { h, tag } from "../ui.js";
import { maskAccount } from "../privacy.js";
import { dataTable } from "./table.js";
import { sizingOf } from "../sizing.js";
import { t } from "../i18n.js";

export const accountKey = (idx, spec) => `${idx}::${spec}`;

/**
 * A table of every known trade account with a "route" switch and the sizing
 * rule per account. `selected` maps accountKey → the routed entry (enabled,
 * sizing). Returns { el, update(known), collect() } where collect() yields the
 * routed entries the API expects ({ token_idx, lid, spec, enabled, qty_multiplier, sizing }).
 */
export function routedAccountsTable({ known, selected, compact = false }) {
  const table = dataTable({
    compact,
    empty: t("No trade accounts discovered yet — add a login under Settings → Broker Accounts and Connect & Verify."),
    columns: [
      { label: t("Route"), render: (a) => h("input", { type: "checkbox", class: "switch acc-on", checked: !!(selected.get(accountKey(a.token_idx, a.spec)) || {}).enabled, dataset: { key: accountKey(a.token_idx, a.spec) } }) },
      { label: t("Login"), render: (a) => a.token_name || "—" },
      { label: t("Account"), render: (a) => h("code", null, maskAccount(a.spec) || "—") },
      { label: t("Env"), render: (a) => tag((a.environment || "—").toUpperCase(), a.environment === "live" ? "live" : "demo") },
      { label: t("Status"), render: (a) => h("span", { class: a.connected ? "pos" : "muted" }, a.connected ? t("connected") : t("offline")) },
      { label: t("Sizing"), render: (a) => { const sz = sizingOf(selected.get(accountKey(a.token_idx, a.spec))); return h("select", { class: "acc-mode input-sm", dataset: { key: accountKey(a.token_idx, a.spec) } },
        h("option", { value: "same", selected: sz.mode === "same" }, t("Same (1:1)")), h("option", { value: "multiplier", selected: sz.mode === "multiplier" }, t("Multiplier")), h("option", { value: "fixed", selected: sz.mode === "fixed" }, t("Fixed"))); } },
      { label: "×", render: (a) => h("input", { type: "number", class: "acc-mult input-sm", min: 0.01, step: 0.01, style: "width:70px", value: sizingOf(selected.get(accountKey(a.token_idx, a.spec))).multiplier, dataset: { key: accountKey(a.token_idx, a.spec) } }) },
      { label: t("Fixed"), render: (a) => h("input", { type: "number", class: "acc-fixed input-sm", min: 1, step: 1, style: "width:64px", value: sizingOf(selected.get(accountKey(a.token_idx, a.spec))).fixed, dataset: { key: accountKey(a.token_idx, a.spec) } }) },
      { label: t("Max"), render: (a) => h("input", { type: "number", class: "acc-max input-sm", min: 0, step: 1, style: "width:64px", title: t("0 = no cap"), value: sizingOf(selected.get(accountKey(a.token_idx, a.spec))).max_contracts, dataset: { key: accountKey(a.token_idx, a.spec) } }) },
    ],
  });
  let rows = known;
  table.update(rows);
  const collect = () => rows.map((a) => {
    const key = accountKey(a.token_idx, a.spec);
    const q = (cls) => table.tbody.querySelector(`.${cls}[data-key="${CSS.escape(key)}"]`);
    const on = q("acc-on");
    const sizing = { mode: q("acc-mode") ? q("acc-mode").value : "same", multiplier: Number(q("acc-mult") && q("acc-mult").value) || 1,
      fixed: Number(q("acc-fixed") && q("acc-fixed").value) || 1, max_contracts: Number(q("acc-max") && q("acc-max").value) || 0 };
    return { token_idx: a.token_idx, lid: a.lid || "", spec: a.spec, enabled: !!(on && on.checked), qty_multiplier: sizing.mode === "multiplier" ? sizing.multiplier : 1, sizing };
  }).filter((a) => a.enabled);
  return { el: table.el, tbody: table.tbody, update: (k) => { rows = k; table.update(k); }, collect };
}
