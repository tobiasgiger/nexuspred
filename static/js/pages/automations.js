/* Settings → Automations: "when <event> [matching …] then <action>" rules the
   bridge runs on its own, plus the log of recent firings. */
import { h, tag, card, pageHead, clear, toast, fmtDateTime, confirmDialog } from "../ui.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { store } from "../store.js";
import { dataTable } from "../components/table.js";
import { openDrawer, closeDrawer } from "../components/drawer.js";
import { maskAccount } from "../privacy.js";
import { t } from "../i18n.js";

const EVENTS = () => ([
  ["position.closed", t("Position closed (with its P&L)")],
  ["position.opened", t("Position opened")],
  ["risk.triggered", t("Risk guard fired")],
  ["execution.problem", t("Execution problem (stop missing, order outcome unknown …)")],
  ["signal.failed", t("Signal received but not executed")],
  ["trade.executed", t("Signal executed")],
  ["connection.lost", t("Connection lost")],
  ["connection.restored", t("Connection restored")],
  ["news.lock", t("News lock started")],
  ["agent.lost", t("Execution agent went offline")],
  ["copy.alert", t("Copy trading alert")],
  ["discord.lost", t("Discord listener went offline")],
  ["daily.summary", t("Daily summary")],
]);
const ACTIONS = () => ([
  ["notify", t("Notify (Discord, email, push)")],
  ["trading_off", t("Switch trading OFF")],
  ["flatten_account", t("Flatten the account")],
  ["lock_account", t("Flatten + lock the account for today")],
  ["flatten_all", t("Flatten everything (all accounts)")],
  ["pause_webhook", t("Pause the webhook")],
]);
const label = (list, key) => (list().find(([k]) => k === key) || [key, key])[1];

function ruleDrawer(rule, onSave) {
  const isNew = !rule;
  const r = { enabled: true, event: "position.closed", action: "notify", accounts: [], symbols: [], webhooks: [], cooldown_s: 60, message: "", loss_at_least: null, ...(rule || {}) };
  const name = h("input", { type: "text", value: r.name || "", maxlength: 80, placeholder: t("e.g. Stop after a big loss") });
  const event = h("select", null, EVENTS().map(([v, l]) => h("option", { value: v, selected: v === r.event }, l)));
  const action = h("select", null, ACTIONS().map(([v, l]) => h("option", { value: v, selected: v === r.action }, l)));
  const accounts = h("div", { class: "check-list" });
  const known = store.get("tradeAccounts") || [];
  const selAcc = new Set(r.accounts || []);
  if (!known.length) accounts.append(h("p", { class: "hint" }, t("No trade accounts discovered yet — every account matches.")));
  for (const a of known) {
    const id = `au-acct-${a.spec}`;
    accounts.append(h("label", { class: "check-row", for: id },
      h("input", { type: "checkbox", id, checked: selAcc.has(a.spec), onChange: (e) => { if (e.target.checked) selAcc.add(a.spec); else selAcc.delete(a.spec); } }),
      h("span", null, h("strong", null, maskAccount(a.spec)), " ", h("span", { class: "muted" }, `${a.token_name} · ${a.environment}`))));
  }
  const webhooks = h("div", { class: "check-list" });
  const selWh = new Set(r.webhooks || []);
  const whs = store.get("webhooks") || [];
  if (!whs.length) webhooks.append(h("p", { class: "hint" }, t("No webhooks yet — every webhook matches.")));
  for (const w of whs) {
    const id = `au-wh-${w.id}`;
    webhooks.append(h("label", { class: "check-row", for: id },
      h("input", { type: "checkbox", id, checked: selWh.has(w.id) || selWh.has(w.name), onChange: (e) => { if (e.target.checked) selWh.add(w.id); else { selWh.delete(w.id); selWh.delete(w.name); } } }),
      h("span", null, h("strong", null, w.name), " ", h("span", { class: "muted" }, w.strategy))));
  }
  const symbols = h("input", { type: "text", value: (r.symbols || []).join(", "), placeholder: "MNQ, ES", autocomplete: "off" });
  const loss = h("input", { type: "number", min: 0, step: 1, value: r.loss_at_least ?? "", placeholder: t("any"), inputmode: "decimal", style: "max-width:160px" });
  const cooldown = h("input", { type: "number", min: 0, max: 86400, step: 1, value: r.cooldown_s ?? 60, style: "max-width:160px" });
  const message = h("textarea", { rows: 3, placeholder: t("Optional. Placeholders: {account} {symbol} {pnl} {webhook} {reason} {event}") }, r.message || "");
  const enabled = h("input", { type: "checkbox", class: "switch", checked: r.enabled !== false });
  const lossField = h("div", { class: "field" }, h("label", null, t("Only when the closed position lost at least (currency)")), loss,
    h("div", { class: "field-hint" }, t("Position closed only. Empty = every close.")));
  const whField = h("div", { class: "field" }, h("label", null, t("Only these webhooks")), webhooks);
  const refresh = () => {
    lossField.hidden = event.value !== "position.closed";
    whField.hidden = !["signal.failed", "trade.executed"].includes(event.value) && action.value !== "pause_webhook";
  };
  event.addEventListener("change", refresh); action.addEventListener("change", refresh); refresh();
  const body = h("div", null,
    h("label", { class: "switch-row" }, h("span", null, t("Enabled")), enabled),
    h("div", { class: "field" }, h("label", null, t("Name")), name),
    h("div", { class: "field" }, h("label", null, t("When")), event),
    h("div", { class: "field" }, h("label", null, t("Only these accounts")), accounts, h("div", { class: "field-hint" }, t("None ticked = every account. Account-scoped actions act on the event's account, else on the ticked ones."))),
    h("div", { class: "field" }, h("label", null, t("Only these symbols (roots)")), symbols),
    lossField, whField,
    h("div", { class: "field" }, h("label", null, t("Then")), action),
    h("div", { class: "field" }, h("label", null, t("Cooldown (seconds)")), cooldown, h("div", { class: "field-hint" }, t("The rule fires at most once per cooldown."))),
    h("div", { class: "field" }, h("label", null, t("Message")), message),
    h("p", { class: "hint" }, t("Flatten and lock actions send market orders on your behalf, without the Trading switch. Every firing is logged under Logs and announced on your alert channels.")));
  const save = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
    const out = { ...r, name: name.value.trim(), event: event.value, action: action.value, accounts: [...selAcc], webhooks: [...selWh],
      symbols: symbols.value.split(",").map((s) => s.trim()).filter(Boolean), loss_at_least: loss.value === "" ? null : Number(loss.value),
      cooldown_s: Number(cooldown.value) || 0, message: message.value, enabled: enabled.checked };
    save.disabled = true;
    try { await onSave(out); closeDrawer(); } catch (e) { toast(e.message, "error"); } finally { save.disabled = false; }
  } }, icon("check"), isNew ? t("Add rule") : t("Save rule"));
  openDrawer({ title: isNew ? t("New automation") : r.name || t("Automation"), body, foot: [save, h("button", { type: "button", class: "btn btn-ghost", onClick: () => closeDrawer() }, t("Cancel"))] });
}

export default {
  title: t("Automations"),
  render(root, { navigate }) {
    let rules = [];
    const filters = (r) => [
      r.accounts.length ? t("accounts: {list}", { list: r.accounts.map(maskAccount).join(", ") }) : null,
      r.symbols.length ? t("symbols: {list}", { list: r.symbols.join(", ") }) : null,
      r.webhooks.length ? t("webhooks: {list}", { list: r.webhooks.join(", ") }) : null,
      r.loss_at_least != null ? t("loss ≥ {n}", { n: r.loss_at_least }) : null,
    ].filter(Boolean).join(" · ") || t("every event");
    const table = dataTable({
      empty: t("No automations yet — add a rule such as “Risk guard fired → switch trading off”."),
      onRow: (r) => ruleDrawer(r, (out) => saveRules(rules.map((x) => (x.id === r.id ? out : x)))),
      columns: [
        { label: t("On"), render: (r) => h("input", { type: "checkbox", class: "switch", checked: r.enabled, onChange: (e) => saveRules(rules.map((x) => (x.id === r.id ? { ...x, enabled: e.target.checked } : x))).catch(() => { e.target.checked = !e.target.checked; }) }) },
        { label: t("Name"), render: (r) => h("strong", null, r.name) },
        { label: t("When"), render: (r) => label(EVENTS, r.event) },
        { label: t("Matching"), render: (r) => h("span", { class: "muted" }, filters(r)) },
        { label: t("Then"), render: (r) => tag(label(ACTIONS, r.action), r.action === "notify" ? "accent" : r.action === "trading_off" || r.action === "pause_webhook" ? "warn" : "red") },
        { label: t("Cooldown"), className: "num", render: (r) => `${r.cooldown_s}s` },
        { label: "", render: (r) => h("button", { type: "button", class: "btn btn-ghost btn-icon btn-sm", title: t("Delete"), onClick: async () => {
          if (!(await confirmDialog({ title: t("Delete this automation?"), body: r.name, confirmText: t("Delete"), danger: true }))) return;
          await saveRules(rules.filter((x) => x.id !== r.id));
        } }, icon("trash")) },
      ],
    });
    const log = dataTable({
      empty: t("Nothing fired yet."),
      columns: [
        { label: t("Time"), render: (e) => fmtDateTime(e.at) },
        { label: t("Rule"), render: (e) => e.name },
        { label: t("Event"), render: (e) => h("span", { class: "muted" }, e.summary || e.event) },
        { label: t("Action"), render: (e) => label(ACTIONS, e.action) },
        { label: t("Result"), render: (e) => h("span", { class: /^failed/.test(e.result || "") ? "neg" : "" }, e.result) },
      ],
    });
    async function paint(r) {
      rules = r.rules || [];
      table.update(rules, { force: true });
      log.update(r.log || []);
    }
    async function load() {
      try { paint(await api.get("/api/automations")); } catch (e) { toast(e.message, "error"); }
    }
    async function saveRules(next) {
      const r = await api.put("/api/automations", { rules: next });
      paint(r); toast(t("Automations saved"), "success");
    }
    root.append(
      pageHead(t("Automations"), t("Rules the bridge runs on its own: when something happens — a position closes with a loss, the risk guard fires, a connection drops — it notifies you, switches trading off, flattens or locks an account, or pauses a webhook. Each rule fires at most once per cooldown; every firing is logged."), [
        h("button", { class: "btn btn-ghost", onClick: () => navigate("/settings/alerts") }, icon("bell"), t("Alert channels")),
        h("button", { class: "btn btn-primary", onClick: () => ruleDrawer(null, (out) => saveRules([...rules, out])) }, icon("plus"), t("Add rule")),
      ]),
      card({ title: t("Rules") }, table.el),
      card({ title: t("Recent firings"), actions: [h("button", { class: "btn btn-ghost btn-sm", onClick: load }, icon("refresh"), t("Refresh"))] }, log.el),
    );
    load();
    const timer = setInterval(load, 15000);
    return () => clearInterval(timer);
  },
};
