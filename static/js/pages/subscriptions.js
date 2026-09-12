/* Subscription journal: what each marketplace subscription / copy follow did
   for you — signals received and their outcomes, and your P&L on the routed
   accounts since you subscribed. */
import { h, tag, card, pageHead, clear, toast, fmtDateTime, fmtTime } from "../ui.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { fmtSigned } from "../charts.js";
import { dataTable } from "../components/table.js";
import { STRATEGY_LABEL } from "../templates.js";
import { openSubscriptionDrawer, openCopySubscriptionDrawer } from "./marketplace.js";
import { t } from "../i18n.js";

const money = (v) => h("span", { class: `pnl ${Number(v) > 0 ? "pos" : Number(v) < 0 ? "neg" : ""}` }, fmtSigned(v, 2));
const pct = (v) => `${Math.round((v || 0) * 100)}%`;

export function recordStrip(rec, { compact = false } = {}) {
  /* One line of figures for a track record (marketplace card, journal card). */
  if (!rec || rec.basis === "none") return h("div", { class: "muted", style: "font-size:12px" }, t("No track record yet."));
  if (!rec.trades) return h("div", { class: "muted", style: "font-size:12px" }, t("No closed trades in the publisher's journal yet."));
  const items = [
    [t("Trades"), String(rec.trades)],
    [t("Win rate"), pct(rec.win_rate)],
    [t("Profit factor"), rec.profit_factor == null ? "∞" : String(rec.profit_factor)],
    [t("Net"), money(rec.net_pnl)],
    [t("30 d"), money(rec.net_30d)],
    [t("Max DD"), money(rec.max_drawdown)],
  ];
  if (!compact) items.push([t("Days"), String(rec.trading_days)]);
  return h("div", { class: "tr-strip" },
    items.map(([k, v]) => h("span", { class: "tr-kv" }, h("span", { class: "k" }, k), h("span", { class: "v" }, v))),
    rec.verified ? tag(t("broker-verified"), "on") : tag(t("{p} verified", { p: pct(rec.verified_share) }), "warn"));
}

export default {
  title: t("Subscription journal"),
  render(root) {
    const list = h("div", { class: "mk-grid" });
    const status = h("p", { class: "hint" }, t("Loading…"));
    const detailTitle = h("h2", null, t("Signals"));
    const signalsTable = dataTable({
      empty: t("No signals received yet."),
      columns: [
        { label: t("Time"), render: (s) => fmtDateTime(s.ts) },
        { label: t("Action"), render: (s) => tag(s.action || "—", (s.action || "").toLowerCase() === "buy" ? "buy" : (s.action || "").toLowerCase() === "sell" ? "sell" : "") },
        { label: t("Symbol"), render: (s) => s.symbol || "—" },
        { label: t("Qty"), className: "num", render: (s) => s.qty == null ? "—" : String(s.qty) },
        { label: t("Result"), render: (s) => tag(s.result, /^error/.test(s.result) ? "rejected" : s.result === "skipped" ? "warn" : s.result === "received" ? "" : "ok") },
      ],
    });
    const copyTable = dataTable({
      empty: t("No copy events for your accounts yet."),
      columns: [
        { label: t("Time"), render: (e) => fmtDateTime(e.ts) },
        { label: t("Kind"), render: (e) => tag(e.kind, e.kind === "reject" || e.kind === "error" ? "rejected" : e.kind === "mirror" ? "ok" : "") },
        { label: t("Symbol"), render: (e) => e.symbol || "—" },
        { label: t("Detail"), render: (e) => e.detail || "—" },
        { label: t("Latency"), className: "num", render: (e) => e.latency_ms == null ? "—" : `${e.latency_ms} ms` },
      ],
    });
    const detail = card({ title: "" }, detailTitle, signalsTable.el, copyTable.el);
    detail.hidden = true;

    async function showJournal(sub, view) {
      try {
        const j = await api.get(`/api/subscriptions/${sub.id}/journal`);
        detail.hidden = false;
        detailTitle.textContent = t("{title} — since {when}", { title: view.title, when: fmtDateTime(j.since) });
        signalsTable.el.hidden = j.kind === "copy";
        copyTable.el.hidden = j.kind !== "copy";
        signalsTable.update(j.recent || [], { force: true });
        copyTable.update(j.copy_events || [], { force: true });
        detail.scrollIntoView({ behavior: "smooth", block: "start" });
      } catch (e) { toast(e.message, "error"); }
    }

    async function load() {
      try {
        const subs = await api.get("/api/subscriptions");
        clear(list);
        status.textContent = "";
        if (!subs.length) {
          list.append(h("div", { class: "card", style: "grid-column:1/-1" }, h("div", { class: "empty-state" }, icon("store"), h("div", null, t("You haven't subscribed to anything yet — browse the Marketplace.")))));
          return;
        }
        const journals = await Promise.all(subs.map((s) => api.get(`/api/subscriptions/${s.id}/journal`).catch(() => null)));
        subs.forEach((s, i) => {
          const j = journals[i];
          const isCopy = s.kind === "copy";
          const view = s.webhook || s.copy || { title: s.webhook_id, publisher_email: "" };
          const sig = j && j.signals;
          const manage = () => (isCopy ? openCopySubscriptionDrawer({ ...(s.copy || {}), subscription: s }, load) : openSubscriptionDrawer({ ...(s.webhook || {}), subscription: s }, load));
          list.append(h("div", { class: "card mk-card" },
            h("div", { class: "mk-title" }, h("strong", null, view.title), isCopy ? tag("copy trading", "accent") : tag(STRATEGY_LABEL[view.strategy] || view.strategy || "—", view.strategy),
              !s.active ? tag(t("inactive"), "warn") : tag(t("active"), "on")),
            h("div", { class: "mk-meta" }, icon("user"), view.publisher_email || "—", "·", icon("calendar"), t("since {when}", { when: fmtDateTime(s.created_at) }),
              "·", icon("users"), t("{n} account(s)", { n: (s.accounts || []).filter((a) => a.enabled !== false).length })),
            j ? h("div", null,
              h("div", { class: "tr-strip" },
                h("span", { class: "tr-kv" }, h("span", { class: "k" }, t("Net since")), h("span", { class: "v" }, money(j.pnl.net_pnl))),
                h("span", { class: "tr-kv" }, h("span", { class: "k" }, t("Trades")), h("span", { class: "v" }, String(j.pnl.trades))),
                h("span", { class: "tr-kv" }, h("span", { class: "k" }, t("Win rate")), h("span", { class: "v" }, pct(j.pnl.win_rate))),
                h("span", { class: "tr-kv" }, h("span", { class: "k" }, t("30 d")), h("span", { class: "v" }, money(j.pnl.net_30d))),
                sig ? h("span", { class: "tr-kv" }, h("span", { class: "k" }, t("Signals")), h("span", { class: "v" }, `${sig.executed} ✓ · ${sig.skipped} ⏭ · ${sig.errors} ✗`)) : null,
                sig && sig.last_at ? h("span", { class: "tr-kv" }, h("span", { class: "k" }, t("Last")), h("span", { class: "v" }, fmtTime(sig.last_at))) : null),
              h("p", { class: "hint", style: "margin:6px 0 0" }, t("P&L = your own journal on the routed accounts since you subscribed (includes anything else those accounts traded)."))) : h("div", { class: "muted" }, t("Journal unavailable")),
            h("div", { class: "mk-foot" }, h("span"),
              h("div", { class: "inline-actions" },
                h("button", { type: "button", class: "btn btn-sm", onClick: () => showJournal(s, view) }, icon("logs"), isCopy ? t("Copy events") : t("Signals")),
                h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: manage }, t("Manage"), icon("chevron"))))));
        });
      } catch (e) { status.textContent = e.message; }
    }
    root.append(
      pageHead(t("Subscription journal"), t("What each subscription did for you: the signals it delivered and how they ended, or the mirrored copy events, and your P&L on the routed accounts since you subscribed."), [
        h("button", { class: "btn", onClick: load }, icon("refresh"), t("Refresh")),
      ]),
      status, list, detail);
    load();
    return () => {};
  },
};
