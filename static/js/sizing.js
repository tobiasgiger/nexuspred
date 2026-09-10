/* Per-account sizing rule shared by the webhook and marketplace account tables. */

/** Sizing of a routed account as the tables edit it — legacy `qty_multiplier` maps to the multiplier mode. */
export function sizingOf(a) {
  const s = (a && a.sizing) || {};
  const legacy = Number(a && a.qty_multiplier) || 1;
  return { mode: s.mode || (legacy !== 1 ? "multiplier" : "same"), multiplier: s.multiplier ?? legacy, fixed: s.fixed ?? 1, max_contracts: s.max_contracts ?? 0 };
}
