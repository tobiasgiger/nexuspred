/* Settings → Tradovate Accounts: token logins + the discovered trade accounts. */
import { h, card, tag, toast, confirmDialog, pageHead } from "../ui.js";
import { maskAccount } from "../privacy.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { store } from "../store.js";
import { actions } from "../actions.js";
import { dataTable } from "../components/table.js";
import { openDrawer, closeDrawer } from "../components/drawer.js";

export default {
  title: "Tradovate Accounts",
  render(root) {
    const tbody = h("tbody");
    let agents = [];  // paired execution agents (admin-managed); loaded once, rows read it
    let dirty = false;
    const markDirty = () => { dirty = true; saveHint.textContent = "Unsaved changes"; saveHint.className = "save-hint"; };

    const secretInput = (cls, value, placeholder) => {
      const inp = h("input", { type: "password", class: `${cls} input-sm`, value: value || "", placeholder, autocomplete: "off", style: "min-width:160px" });
      const eye = h("button", { type: "button", class: "btn btn-ghost btn-icon btn-sm", title: "Show / hide", onClick: () => { const show = inp.type === "password"; inp.type = show ? "text" : "password"; eye.replaceChildren(icon(show ? "eyeOff" : "eye")); } }, icon("eye"));
      return h("div", { style: "display:flex;gap:4px;align-items:center" }, inp, eye);
    };
    const row = (a = {}) => {
      const tr = h("tr", { dataset: { lid: a.lid || "" } },
        h("td", null, h("input", { type: "checkbox", class: "switch ta-enabled", checked: !!a.enabled, title: "Login enabled" })),
        h("td", null, h("input", { class: "ta-name input-sm", value: a.name || "", placeholder: "Account 1", style: "min-width:120px" })),
        h("td", null, h("select", { class: "ta-env input-sm", style: "min-width:90px" }, h("option", { value: "demo", selected: a.environment !== "live" }, "Demo"), h("option", { value: "live", selected: a.environment === "live" }, "Live"))),
        h("td", null, secretInput("ta-access", a.access_token, "access token")),
        h("td", null, secretInput("ta-md", a.md_token, "check token (optional)")),
        h("td", null, h("input", { type: "number", class: "ta-mult input-sm", min: 0.1, step: 0.1, value: a.qty_multiplier ?? 1, style: "width:70px" })),
        h("td", null, h("select", { class: "ta-agent input-sm", style: "min-width:120px", title: "Execute this login's Tradovate calls through a paired agent (own IP) or directly from the bridge" },
          h("option", { value: "0", selected: !a.agent_id }, "Bridge (direct)"),
          agents.map((g) => h("option", { value: String(g.id), selected: Number(a.agent_id) === g.id }, `${g.name}${g.online ? "" : " (offline)"}`)),
          a.agent_id && !agents.some((g) => g.id === Number(a.agent_id)) ? h("option", { value: String(a.agent_id), selected: true }, `Agent #${a.agent_id} (revoked)`) : null)),
        h("td", null, a.token_expires ? h("span", { class: "muted nowrap" }, new Date(a.token_expires).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })) : h("span", { class: "muted" }, "—")),
        h("td", { style: "width:44px" }, h("button", { type: "button", class: "btn btn-ghost btn-icon", title: "Remove login", onClick: async () => {
          if (a.name && !(await confirmDialog({ title: `Remove login "${a.name}"?`, body: "Its token is dropped and every webhook routed to its accounts loses that route after you save.", confirmText: "Remove", danger: true }))) return;
          tr.remove(); markDirty();
        } }, icon("trash"))));
      tr.addEventListener("input", markDirty);
      tr.addEventListener("change", markDirty);
      return tr;
    };
    const paint = (list) => { tbody.replaceChildren(); tbody.append(...(list && list.length ? list : [{}]).map(row)); dirty = false; saveHint.textContent = ""; };
    const collect = () => [...tbody.querySelectorAll("tr")].map((tr) => ({
      lid: tr.dataset.lid || "",
      enabled: tr.querySelector(".ta-enabled").checked,
      name: tr.querySelector(".ta-name").value.trim(),
      environment: tr.querySelector(".ta-env").value,
      access_token: tr.querySelector(".ta-access").value.trim(),
      md_token: tr.querySelector(".ta-md").value.trim(),
      qty_multiplier: Number(tr.querySelector(".ta-mult").value) || 1,
      agent_id: Number(tr.querySelector(".ta-agent").value) || 0,
    })).filter((a) => a.name || a.access_token);

    const saveHint = h("span", { class: "save-hint" });
    const saveBtn = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
      try {
        const list = await api.post("/api/token-accounts", collect());
        dirty = false;
        store.set("tokenAccounts", list);
        paint(list);
        toast("Token accounts saved", "success");
        actions.refreshStatus(); actions.loadTradeAccounts();
      } catch (e) { toast(e.message, "error"); }
    } }, icon("check"), "Save logins");
    const connectBtn = h("button", { type: "button", class: "btn btn-secondary", onClick: async () => {
      if (dirty) { toast("Save your logins first", "warn"); return; }
      await actions.connectAll();
    } }, icon("refresh"), "Connect & Verify");

    const money = (v) => Number(v).toLocaleString([], { maximumFractionDigits: 0 });
    const riskSummary = (a) => {
      const r = a.risk || {};
      const parts = [];
      if (Number(r.loss_limit)) parts.push(`−${money(r.loss_limit)}`);
      if (Number(r.profit_limit)) parts.push(`+${money(r.profit_limit)}`);
      if (r.flatten_at) parts.push(`⏰ ${r.flatten_at}${r.flatten_tz === "ny" ? " NY" : ""}`);
      return parts.length ? parts.join(" · ") : "";
    };
    /** Drawer: daily loss / profit limit, flatten time and today's lock for one account. */
    function riskDrawer(a) {
      const r = a.risk || {};
      const lossInp = h("input", { type: "number", min: 0, step: 1, value: r.loss_limit || "", placeholder: "off" });
      const profitInp = h("input", { type: "number", min: 0, step: 1, value: r.profit_limit || "", placeholder: "off" });
      const timeInp = h("input", { type: "time", value: r.flatten_at || "" });
      const tzSel = h("select", null, h("option", { value: "local", selected: (r.flatten_tz || "local") === "local" }, "Journal timezone"), h("option", { value: "ny", selected: r.flatten_tz === "ny" }, "New York (exchange time)"));
      const lock = a.locked;
      const lockBox = lock
        ? h("div", { class: "callout warn" }, h("strong", null, "Locked for today: "), lock.reason, h("div", { class: "hint", style: "margin-top:4px" }, `Since ${new Date(lock.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} · P&L at trigger ${Number(lock.pnl).toLocaleString([], { maximumFractionDigits: 2 })}. Bridge orders for this account are refused; a position that reappears is closed again. The lock ends with the Tradovate trading day (17:00 New York).`))
        : h("div", { class: "callout" }, "Not locked. When a rule fires, every working order is cancelled, every position closed at market and the account locked until the next local day.");
      const unlockBtn = lock ? h("button", { type: "button", class: "btn btn-ghost btn-danger", onClick: async () => {
        if (!(await confirmDialog({ title: `Unlock ${maskAccount(a.spec)}?`, body: "Bridge orders are accepted again today. The rules stay in place and can fire again.", confirmText: "Unlock", danger: true }))) return;
        try { await api.post("/api/risk/unlock", { spec: a.spec }); toast("Account unlocked", "success"); closeDrawer(); actions.loadTradeAccounts(); }
        catch (e) { toast(e.message, "error"); }
      } }, icon("key"), "Unlock for today") : null;
      const saveRisk = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
        saveRisk.disabled = true;
        try {
          const list = await api.post("/api/trade-accounts", [{ token_idx: a.token_idx, spec: a.spec, id: a.id, enabled: a.enabled, qty_multiplier: a.qty_multiplier,
            risk: { loss_limit: Number(lossInp.value) || 0, profit_limit: Number(profitInp.value) || 0, flatten_at: timeInp.value || "", flatten_tz: tzSel.value } }]);
          store.set("tradeAccounts", list);
          toast("Risk rules saved", "success");
          closeDrawer();
        } catch (e) { toast(e.message, "error"); }
        finally { saveRisk.disabled = false; }
      } }, icon("check"), "Save rules");
      openDrawer({
        title: `Risk guard · ${maskAccount(a.spec)}`,
        body: [
          lockBox,
          h("div", { class: "field" }, h("label", null, "Daily loss limit"), lossInp, h("div", { class: "field-hint" }, "Account currency. Fires when today's P&L (realised + open, the broker's figures) reaches −limit. 0 = off.")),
          h("div", { class: "field" }, h("label", null, "Daily profit target"), profitInp, h("div", { class: "field-hint" }, "Fires when today's P&L reaches +target — locks in the day. 0 = off.")),
          h("div", { class: "field" }, h("label", null, "Flatten at"), h("div", { style: "display:flex;gap:8px;flex-wrap:wrap" }, timeInp, tzSel), h("div", { class: "field-hint" }, "Everything on this account is closed at that time and the account is locked for the rest of the trading day. New York time follows the exchange through the daylight-saving weeks; the journal timezone is your own clock. Empty = off.")),
          h("p", { class: "hint" }, "Checked on every live P&L poll (Settings → General → Live P&L refresh, at least every few seconds while a rule is set). Applies to every path that trades this account: webhooks, Discord signals, marketplace subscriptions and copy trading."),
        ],
        foot: [saveRisk, h("button", { type: "button", class: "btn btn-ghost", onClick: () => closeDrawer() }, "Close"), h("span", { style: "flex:1" }), unlockBtn],
      });
    }
    const discovered = dataTable({
      empty: "No accounts yet — add a login above, save, then Connect & Verify.",
      columns: [
        { label: "Login", render: (a) => a.token_name || "—" },
        { label: "Account", render: (a) => h("code", null, maskAccount(a.spec) || "—") },
        { label: "Env", render: (a) => tag((a.environment || "—").toUpperCase(), a.environment === "live" ? "live" : "demo") },
        { label: "Login enabled", render: (a) => h("span", { class: a.token_enabled ? "pos" : "muted" }, a.token_enabled ? "yes" : "no") },
        { label: "Status", render: (a) => h("span", { class: a.connected ? "pos" : "neg" }, a.connected ? "Connected" : "Not connected") },
        { label: "Risk guard", render: (a) => h("span", { class: "inline-actions" },
          a.locked ? tag("locked", "warn") : null,
          h("button", { type: "button", class: "btn btn-ghost btn-sm", title: "Daily loss / profit limit, flatten time", onClick: () => riskDrawer(a) }, icon("shield"), riskSummary(a) || "Set rules")) },
      ],
    });

    root.append(
      pageHead("Tradovate Accounts", "One row per Tradovate login, each with its own access token (auto-renewed). Every signal fans out to the accounts a webhook routes to, in parallel.", [
        h("button", { type: "button", class: "btn", onClick: () => { tbody.append(row()); markDirty(); } }, icon("plus"), "Add login"),
      ]),
      card({ title: "Token logins", hint: "Paste the access token (and optionally the check token) from the Tradovate web trader — see Tools for the extractor. Masked tokens keep their stored value when you save." },
        h("div", { class: "table-scroll" }, h("table", { class: "data-table" },
          h("thead", null, h("tr", null, h("th", null, "On"), h("th", null, "Name"), h("th", null, "Env"), h("th", null, "Access token"), h("th", null, "Check token"), h("th", null, "Qty ×"), h("th", null, "Execute via"), h("th", null, "Token expires"), h("th"))), tbody)),
        h("div", { class: "form-actions", style: "margin-top:12px" }, saveBtn, connectBtn, saveHint)),
      card({ title: "Discovered trade accounts", hint: "Every trade account found under your logins — one login can hold several. Which accounts a signal actually trades is chosen per webhook (Webhooks → Accounts tab)." }, discovered.el),
    );

    api.get("/api/agents").then((list) => { agents = list || []; if (!dirty) paint(store.get("tokenAccounts")); }).catch(() => {});
    const unsubs = [
      store.subscribe("tokenAccounts", (list) => { if (!dirty) paint(list); }, { immediate: true }),
      store.subscribe("tradeAccounts", (list) => discovered.update(list || []), { immediate: true }),
    ];
    actions.loadTokenAccounts();
    actions.loadTradeAccounts();
    return () => unsubs.forEach((u) => u());
  },
};
