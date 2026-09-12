/* Marketplace: signals other users published; subscribe with your own accounts. */
import { h, card, tag, toast, confirmDialog, pageHead, clear } from "../ui.js";
import { maskAccount } from "../privacy.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { store } from "../store.js";
import { actions } from "../actions.js";
import { dataTable } from "../components/table.js";
import { openDrawer, closeDrawer } from "../components/drawer.js";
import { accountKey, routedAccountsTable } from "../components/accounts.js";
import { STRATEGY_LABEL } from "../templates.js";
import { sizingOf } from "../sizing.js";
import { t } from "../i18n.js";


/**
 * Subscribe / edit-subscription drawer.
 * item: {publisher_area_id, webhook_id, title, description, strategy, publisher_email, subscription|null}
 * onDone(): called after save / unsubscribe.
 */
export function openSubscriptionDrawer(item, onDone) {
  const sub = item.subscription || null;
  const known = store.get("tradeAccounts") || [];
  const selected = new Map(((sub && sub.accounts) || []).map((a) => [accountKey(a.token_idx, a.spec), a]));
  const enabledSw = h("input", { type: "checkbox", class: "switch", checked: sub ? !!sub.enabled : true });
  const accTable = routedAccountsTable({ known, selected });
  const collect = accTable.collect;

  const saveBtn = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
    const body = { enabled: enabledSw.checked, accounts: collect() };
    if (!body.accounts.length && enabledSw.checked) {
      if (!(await confirmDialog({ title: t("No accounts routed"), body: t("The subscription will be active but trade on no account. Save anyway?"), confirmText: t("Save") }))) return;
    }
    saveBtn.disabled = true;
    try {
      if (sub) await api.put(`/api/subscriptions/${sub.id}`, body);
      else await api.post(`/api/marketplace/${item.publisher_area_id}/${item.webhook_id}/subscribe`, body);
      toast(sub ? t("Subscription saved") : t("Subscribed to {title}", { title: item.title }), "success");
      closeDrawer();
      if (onDone) onDone();
    } catch (e) { toast(e.message, "error"); } finally { saveBtn.disabled = false; }
  } }, icon("check"), sub ? t("Save") : t("Subscribe"));
  const unsubBtn = sub ? h("button", { type: "button", class: "btn btn-danger", onClick: async () => {
    if (!(await confirmDialog({ title: t("Unsubscribe from \"{title}\"?", { title: item.title }), body: t("Future signals from this publisher won't reach your accounts. Open positions are not touched."), confirmText: t("Unsubscribe"), danger: true }))) return;
    try { await api.del(`/api/subscriptions/${sub.id}`); toast(t("Unsubscribed"), "success"); closeDrawer(); if (onDone) onDone(); }
    catch (e) { toast(e.message, "error"); }
  } }, icon("trash"), t("Unsubscribe")) : null;

  openDrawer({
    title: item.title,
    body: [
      h("div", { class: "callout" },
        h("div", null, h("strong", null, t("Publisher: ")), item.publisher_email || "—", " · ", h("strong", null, t("Strategy: ")), tag(STRATEGY_LABEL[item.strategy] || item.strategy, item.strategy)),
        item.description ? h("div", { style: "margin-top:6px;white-space:pre-line" }, item.description) : null),
      h("label", { class: "switch-row" }, h("span", null, t("Subscription active"), h("small", null, t("Off = signals from this publisher are ignored for your accounts. Your own Trading switch applies as well."))), enabledSw),
      h("h3", null, t("Trade on my accounts")),
      h("p", { class: "hint" }, t("Signals execute on every routed account below, in parallel, sized per account (Same 1:1, Multiplier, or Fixed contracts with an optional Max). The publisher never sees your accounts.")),
      accTable.el,
    ],
    foot: [saveBtn, h("button", { type: "button", class: "btn btn-ghost", onClick: () => closeDrawer() }, t("Close")), h("span", { style: "flex:1" }), unsubBtn],
  });
}

/**
 * Follow a published copy group with your own accounts (copy sizing rules).
 * item: {publisher_area_id, group_id, title, description, symbols, environment, publisher_email, subscription|null}
 */
export function openCopySubscriptionDrawer(item, onDone) {
  const sub = item.subscription || null;
  const known = store.get("tradeAccounts") || [];
  const selected = new Map(((sub && sub.accounts) || []).map((a) => [String(a.spec), a]));
  const enabledSw = h("input", { type: "checkbox", class: "switch", checked: sub ? !!sub.enabled : true });
  const q = (cls, spec) => accTable.tbody.querySelector(`.${cls}[data-spec="${CSS.escape(spec)}"]`);
  const accTable = dataTable({
    compact: true,
    empty: t("No trade accounts discovered yet — add a login under Settings → Broker Accounts and Connect & Verify."),
    columns: [
      { label: t("Follow"), render: (a) => h("input", { type: "checkbox", class: "switch cs-on", checked: selected.has(String(a.spec)) && (selected.get(String(a.spec)).enabled !== false), dataset: { spec: a.spec } }) },
      { label: t("Account"), render: (a) => h("span", null, h("code", null, maskAccount(a.spec)), h("small", { class: "muted", style: "display:block" }, `${a.token_name} · ${(a.environment || "").toUpperCase()}`)) },
      { label: t("Mode"), render: (a) => { const f = selected.get(String(a.spec)) || {}; return h("select", { class: "cs-mode input-sm", dataset: { spec: a.spec } },
        h("option", { value: "multiplier", selected: (f.mode || "multiplier") === "multiplier" }, t("Multiplier")), h("option", { value: "fixed", selected: f.mode === "fixed" }, t("Fixed"))); } },
      { label: "×", render: (a) => h("input", { type: "number", class: "cs-mult input-sm", min: 0.01, step: 0.01, style: "width:70px", value: (selected.get(String(a.spec)) || {}).multiplier ?? 1, dataset: { spec: a.spec } }) },
      { label: t("Fixed"), render: (a) => h("input", { type: "number", class: "cs-fixed input-sm", min: 1, step: 1, style: "width:64px", value: (selected.get(String(a.spec)) || {}).fixed ?? 1, dataset: { spec: a.spec } }) },
      { label: t("Max"), render: (a) => h("input", { type: "number", class: "cs-max input-sm", min: 0, step: 1, style: "width:64px", title: t("0 = no cap"), value: (selected.get(String(a.spec)) || {}).max_contracts ?? 0, dataset: { spec: a.spec } }) },
      { label: t("Direction"), render: (a) => { const f = selected.get(String(a.spec)) || {}; return h("select", { class: "cs-dir input-sm", dataset: { spec: a.spec } },
        ["both", "long", "short"].map((d) => h("option", { value: d, selected: (f.direction || "both") === d }, d))); } },
    ],
  });
  accTable.update(known);
  const collect = () => known.map((a) => {
    const on = q("cs-on", a.spec);
    if (!on || !on.checked) return null;
    return { spec: a.spec, lid: a.lid || "", token_idx: a.token_idx, account_id: a.id, enabled: true,
      mode: q("cs-mode", a.spec).value, multiplier: Number(q("cs-mult", a.spec).value) || 1, fixed: Number(q("cs-fixed", a.spec).value) || 1,
      max_contracts: Number(q("cs-max", a.spec).value) || 0, direction: q("cs-dir", a.spec).value };
  }).filter(Boolean);
  const saveBtn = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
    const body = { enabled: enabledSw.checked, accounts: collect() };
    if (!body.accounts.length) return toast(t("Switch on at least one account"), "error");
    saveBtn.disabled = true;
    try {
      if (sub) await api.put(`/api/subscriptions/${sub.id}`, body);
      else await api.post(`/api/marketplace/${item.publisher_area_id}/copy/${item.group_id}/subscribe`, body);
      toast(sub ? t("Copy subscription saved") : t("Following {title}", { title: item.title }), "success");
      closeDrawer();
      if (onDone) onDone();
    } catch (e) { toast(e.message, "error"); } finally { saveBtn.disabled = false; }
  } }, icon("check"), sub ? t("Save") : t("Follow"));
  const unsubBtn = sub ? h("button", { type: "button", class: "btn btn-danger", onClick: async () => {
    if (!(await confirmDialog({ title: t("Stop following \"{title}\"?", { title: item.title }), body: t("Your accounts leave the mirror. Positions they hold are NOT closed — flatten them yourself if you want to be flat."), confirmText: t("Stop following"), danger: true }))) return;
    try { await api.del(`/api/subscriptions/${sub.id}`); toast(t("Stopped following"), "success"); closeDrawer(); if (onDone) onDone(); }
    catch (e) { toast(e.message, "error"); }
  } }, icon("trash"), t("Stop following")) : null;
  openDrawer({
    title: item.title,
    width: "760px",
    body: [
      h("div", { class: "callout" },
        h("div", null, h("strong", null, t("Leader: ")), item.publisher_email || "—", " · ", tag("copy trading", "accent"), " ", tag((item.environment || "demo").toUpperCase(), item.environment === "live" ? "live" : "demo"),
          (item.symbols || []).length ? [" · ", h("strong", null, t("Symbols: ")), item.symbols.join(", ")] : [" · ", h("span", { class: "muted" }, t("every contract the leader trades"))]),
        item.description ? h("div", { style: "margin-top:6px;white-space:pre-line" }, item.description) : null),
      h("label", { class: "switch-row" }, h("span", null, t("Subscription active"), h("small", null, t("Off = your accounts leave the mirror (positions stay). Your own Trading switch and risk locks apply as well."))), enabledSw),
      h("h3", null, t("Follow with my accounts")),
      h("p", { class: "hint" }, t("Every position change of the leader is mirrored onto the accounts below, live, at market. Multiplier: leader size × factor. Fixed: this many contracts per leader entry. Max caps the size; Direction copies only longs or only shorts. A follower account is exclusive: do not trade it by hand or through another route. The leader never sees your accounts.")),
      accTable.el,
      h("div", { class: "callout warn", style: "margin-top:10px" }, t("Positions the leader already holds when you start following are not copied (baseline). Mirroring of such a contract begins once the leader is flat again. If the leader's feed is lost, the group's feed-loss rule applies to your accounts too (flatten or pause).")),
    ],
    foot: [saveBtn, h("button", { type: "button", class: "btn btn-ghost", onClick: () => closeDrawer() }, t("Close")), h("span", { style: "flex:1" }), unsubBtn],
  });
}

export default {
  title: t("Marketplace"),
  render(root, { navigate }) {
    const grid = h("div", { class: "mk-grid" });
    const status = h("p", { class: "hint" }, t("Loading…"));

    async function load() {
      try {
        const items = await api.get("/api/marketplace");
        clear(grid);
        status.textContent = items.length ? "" : "";
        if (!items.length) {
          grid.append(h("div", { class: "card", style: "grid-column:1/-1" }, h("div", { class: "empty-state" }, icon("store"), h("div", null, t("No signals are published for your account right now.")))));
          return;
        }
        for (const it of items) {
          const sub = it.subscription;
          const isCopy = it.kind === "copy";
          const live = isCopy ? (!it.enabled ? tag("group off", "warn") : it.paused ? tag("paused", "warn") : it.running && it.feed_ok ? tag("live", "on") : it.running ? tag("feed lost", "off") : tag("starting", "")) : null;
          const state = !sub ? tag("not subscribed") : (isCopy ? !it.enabled : !it.webhook_enabled) ? tag("paused by publisher", "warn") : sub.enabled ? tag(isCopy ? t("following · on") : t("subscribed · on"), "on") : tag(isCopy ? t("following · off") : t("subscribed · off"), "off");
          const open = () => (isCopy ? openCopySubscriptionDrawer(it, load) : openSubscriptionDrawer(it, load));
          grid.append(h("div", { class: "card mk-card" },
            h("div", { class: "mk-title" }, h("strong", null, it.title), isCopy ? tag("copy trading", "accent") : tag(STRATEGY_LABEL[it.strategy] || it.strategy, it.strategy),
              isCopy ? tag((it.environment || "demo").toUpperCase(), it.environment === "live" ? "live" : "demo") : null),
            h("div", { class: "mk-desc" }, it.description || (isCopy ? `Mirrors the leader's positions live${(it.symbols || []).length ? ` (${it.symbols.join(", ")})` : ""}.` : "No description.")),
            h("div", { class: "mk-meta" }, icon("user"), it.publisher_email || "—", "·", icon("users"), `${it.subscriber_count} ${isCopy ? "follower" : "subscriber"}${it.subscriber_count === 1 ? "" : "s"}`,
              live ? ["·", live] : null,
              it.visibility === "selected" ? ["·", tag("invite-only", "accent")] : null),
            h("div", { class: "mk-foot" }, state,
              h("div", { class: "inline-actions" },
                sub ? h("input", { type: "checkbox", class: "switch", checked: !!sub.enabled, title: t("Enable / disable"), onChange: async (e) => {
                  try { await api.put(`/api/subscriptions/${sub.id}`, { enabled: e.target.checked }); toast(e.target.checked ? t("Subscription enabled") : t("Subscription disabled"), "success"); load(); }
                  catch (err) { e.target.checked = !e.target.checked; toast(err.message, "error"); }
                } }) : null,
                h("button", { type: "button", class: `btn btn-sm ${sub ? "" : "btn-primary"}`, onClick: open }, sub ? t("Manage") : isCopy ? t("Follow") : t("Subscribe"))))));
        }
      } catch (e) {
        status.textContent = e.message;
      }
    }

    root.append(
      pageHead(t("Marketplace"), t("Signals and copy-trading leaders other users have published. Subscribe to run a signal on your own trade accounts, or follow a leader whose positions are mirrored onto your accounts live — with your own sizing, your own Trading switch and your own logs and alerts. Publishers never see your accounts."), [
        h("button", { class: "btn", onClick: load }, icon("refresh"), t("Refresh")),
        h("button", { class: "btn btn-ghost", onClick: () => navigate("/webhooks") }, t("My subscriptions"), icon("chevron")),
      ]),
      status, grid,
    );
    if (!(store.get("tradeAccounts") || []).length) actions.loadTradeAccounts();
    load();
    return () => {};
  },
};
