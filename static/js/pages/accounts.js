/* Settings → Broker Accounts: broker logins (Tradovate / Rithmic / ProjectX) + the discovered trade accounts. */
import { h, card, tag, toast, confirmDialog, pageHead } from "../ui.js";
import { maskAccount } from "../privacy.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { store } from "../store.js";
import { actions } from "../actions.js";
import { dataTable } from "../components/table.js";
import { openDrawer, closeDrawer } from "../components/drawer.js";
import { t, locale } from "../i18n.js";

export default {
  title: t("Broker Accounts"),
  render(root) {
    const tbody = h("tbody");
    let agents = [];  // paired execution agents (admin-managed); loaded once, rows read it
    let dirty = false;
    const markDirty = () => { dirty = true; saveHint.textContent = t("Unsaved changes"); saveHint.className = "save-hint"; };

    const secretInput = (cls, value, placeholder) => {
      const inp = h("input", { type: "password", class: `${cls} input-sm`, value: value || "", placeholder, autocomplete: "off", style: "min-width:160px" });
      const eye = h("button", { type: "button", class: "btn btn-ghost btn-icon btn-sm", title: t("Show / hide"), onClick: () => { const show = inp.type === "password"; inp.type = show ? "text" : "password"; eye.replaceChildren(icon(show ? "eyeOff" : "eye")); } }, icon("eye"));
      return h("div", { style: "display:flex;gap:4px;align-items:center" }, inp, eye);
    };
    const row = (a = {}) => {
      const isR = a.broker === "rithmic", isP = a.broker === "projectx";
      const brokerSel = h("select", { class: "ta-broker input-sm", style: "min-width:100px", title: t("Broker this login belongs to") },
        h("option", { value: "tradovate", selected: !isR && !isP }, t("Tradovate")), h("option", { value: "projectx", selected: isP }, t("ProjectX (Topstep …) beta")), h("option", { value: "rithmic", selected: isR }, t("Rithmic (beta)")));
      const pCells = h("div", { class: isP ? "" : "hidden", style: "display:flex;gap:6px;flex-wrap:wrap;align-items:center" },
        h("input", { class: "ta-pxuser input-sm", value: a.px_user || "", placeholder: t("ProjectX user name"), autocomplete: "off", style: "min-width:140px" }),
        secretInput("ta-pxkey", a.px_api_key, "API key"),
        h("input", { class: "ta-pxfirm input-sm", value: a.px_firm || "topstep", placeholder: t("firm (topstep, bulenox …)"), list: "px-firms", style: "min-width:140px", title: t("The prop firm's ProjectX gateway: topstep, alphaticks, bulenox, blusky, e8x, tradeify … or a full https:// URL") }));
      // Tradovate: access + check token. Rithmic: user, password, system, gateway.
      const tvCells = h("div", { class: (isR || isP) ? "hidden" : "", style: "display:flex;gap:6px;flex-wrap:wrap" },
        secretInput("ta-access", a.access_token, "access token"), secretInput("ta-md", a.md_token, "check token (optional)"));
      const rCells = h("div", { class: isR ? "" : "hidden", style: "display:flex;gap:6px;flex-wrap:wrap;align-items:center" },
        h("input", { class: "ta-ruser input-sm", value: a.rithmic_user || "", placeholder: t("Rithmic user"), autocomplete: "off", style: "min-width:120px" }),
        secretInput("ta-rpw", a.rithmic_password, "password"),
        h("input", { class: "ta-rsys input-sm", value: a.rithmic_system || "", placeholder: t("system (Apex, TopstepTrader …)"), list: "rithmic-systems", style: "min-width:150px", title: t("The system name Rithmic gave your prop firm / broker. Demo = Rithmic Paper Trading when empty.") }),
        h("input", { class: "ta-rgw input-sm", value: a.rithmic_gateway || "", placeholder: t("gateway (chicago / europe / paper)"), list: "rithmic-gateways", style: "min-width:150px", title: t("chicago (default live), europe, paper, test — or a full wss:// URL") }));
      const tr = h("tr", { dataset: { lid: a.lid || "" } },
        h("td", null, h("input", { type: "checkbox", class: "switch ta-enabled", checked: !!a.enabled, title: t("Login enabled") })),
        h("td", null, h("input", { class: "ta-name input-sm", value: a.name || "", placeholder: t("Account 1"), style: "min-width:120px" })),
        h("td", null, brokerSel),
        h("td", null, h("select", { class: "ta-env input-sm", style: "min-width:90px" }, h("option", { value: "demo", selected: a.environment !== "live" }, t("Demo")), h("option", { value: "live", selected: a.environment === "live" }, t("Live")))),
        h("td", { colspan: 2 }, tvCells, rCells, pCells),
        h("td", null, h("input", { type: "number", class: "ta-mult input-sm", min: 0.1, step: 0.1, value: a.qty_multiplier ?? 1, style: "width:70px" })),
        h("td", null, h("select", { class: "ta-agent input-sm", style: "min-width:120px", title: t("Execute this login's broker calls through a paired agent (own IP) or directly from the bridge — Tradovate logins only") },
          h("option", { value: "0", selected: !a.agent_id }, t("Bridge (direct)")),
          agents.map((g) => h("option", { value: String(g.id), selected: Number(a.agent_id) === g.id }, `${g.name}${g.online ? "" : t(" (offline)")}`)),
          a.agent_id && !agents.some((g) => g.id === Number(a.agent_id)) ? h("option", { value: String(a.agent_id), selected: true }, t("Agent #{id} (revoked)", { id: a.agent_id })) : null)),
        h("td", null, a.token_expires ? h("span", { class: "muted nowrap" }, new Date(a.token_expires).toLocaleString(locale(), { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })) : h("span", { class: "muted" }, "—")),
        h("td", { style: "width:44px" }, h("button", { type: "button", class: "btn btn-ghost btn-icon", title: t("Remove login"), onClick: async () => {
          if (a.name && !(await confirmDialog({ title: t("Remove login \"{name}\"?", { name: a.name }), body: t("Its token is dropped and every webhook routed to its accounts loses that route after you save."), confirmText: t("Remove"), danger: true }))) return;
          tr.remove(); markDirty();
        } }, icon("trash"))));
      brokerSel.addEventListener("change", () => { const v = brokerSel.value; tvCells.classList.toggle("hidden", v !== "tradovate"); rCells.classList.toggle("hidden", v !== "rithmic"); pCells.classList.toggle("hidden", v !== "projectx"); });
      tr.addEventListener("input", markDirty);
      tr.addEventListener("change", markDirty);
      return tr;
    };
    const paint = (list) => { tbody.replaceChildren(); tbody.append(...(list && list.length ? list : [{}]).map(row)); dirty = false; saveHint.textContent = ""; };
    const collect = () => [...tbody.querySelectorAll("tr")].map((tr) => ({
      lid: tr.dataset.lid || "",
      enabled: tr.querySelector(".ta-enabled").checked,
      name: tr.querySelector(".ta-name").value.trim(),
      broker: tr.querySelector(".ta-broker").value,
      environment: tr.querySelector(".ta-env").value,
      access_token: tr.querySelector(".ta-access").value.trim(),
      md_token: tr.querySelector(".ta-md").value.trim(),
      rithmic_user: tr.querySelector(".ta-ruser").value.trim(),
      rithmic_password: tr.querySelector(".ta-rpw").value.trim(),
      rithmic_system: tr.querySelector(".ta-rsys").value.trim(),
      rithmic_gateway: tr.querySelector(".ta-rgw").value.trim(),
      px_user: tr.querySelector(".ta-pxuser").value.trim(),
      px_api_key: tr.querySelector(".ta-pxkey").value.trim(),
      px_firm: tr.querySelector(".ta-pxfirm").value.trim(),
      qty_multiplier: Number(tr.querySelector(".ta-mult").value) || 1,
      agent_id: Number(tr.querySelector(".ta-agent").value) || 0,
    })).filter((a) => a.name || a.access_token || a.rithmic_user || a.px_user);

    const saveHint = h("span", { class: "save-hint" });
    const saveBtn = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
      if (saveBtn.disabled) return;
      saveBtn.disabled = true;
      try {
        const list = await api.post("/api/token-accounts", collect());
        dirty = false;
        store.set("tokenAccounts", list);
        paint(list);
        toast("Token accounts saved", "success");
        actions.refreshStatus(); actions.loadTradeAccounts();
      } catch (e) { toast(e.message, "error"); }
      finally { saveBtn.disabled = false; }
    } }, icon("check"), t("Save logins"));
    const connectBtn = h("button", { type: "button", class: "btn btn-secondary", onClick: async () => {
      if (dirty) { toast("Save your logins first", "warn"); return; }
      if (connectBtn.disabled) return;
      connectBtn.disabled = true;
      try { await actions.connectAll(); } finally { connectBtn.disabled = false; }
    } }, icon("refresh"), t("Connect & Verify"));

    const money = (v) => Number(v).toLocaleString(locale(), { maximumFractionDigits: 0 });
    const riskSummary = (a) => {
      const r = a.risk || {};
      const parts = [];
      if (Number(r.loss_limit)) parts.push(`−${money(r.loss_limit)}`);
      if (Number(r.profit_limit)) parts.push(`+${money(r.profit_limit)}`);
      if (r.flatten_at) parts.push(`⏰ ${r.flatten_at}${r.flatten_tz === "ny" ? t(" NY") : ""}`);
      return parts.length ? parts.join(" · ") : "";
    };
    /** Drawer: daily loss / profit limit, flatten time and today's lock for one account. */
    function riskDrawer(a) {
      const r = a.risk || {};
      const lossInp = h("input", { type: "number", min: 0, step: 1, value: r.loss_limit || "", placeholder: "off" });
      const profitInp = h("input", { type: "number", min: 0, step: 1, value: r.profit_limit || "", placeholder: "off" });
      const timeInp = h("input", { type: "time", value: r.flatten_at || "" });
      const tzSel = h("select", null, h("option", { value: "local", selected: (r.flatten_tz || "local") === "local" }, t("Journal timezone")), h("option", { value: "ny", selected: r.flatten_tz === "ny" }, t("New York (exchange time)")));
      const lock = a.locked;
      const lockBox = lock
        ? h("div", { class: "callout warn" }, h("strong", null, t("Locked for today: ")), lock.reason, h("div", { class: "hint", style: "margin-top:4px" }, `Since ${new Date(lock.at).toLocaleTimeString(locale(), { hour: "2-digit", minute: "2-digit" })} · P&L at trigger ${Number(lock.pnl).toLocaleString(locale(), { maximumFractionDigits: 2 })}. Bridge orders for this account are refused; a position that reappears is closed again. The lock ends with the Tradovate trading day (17:00 New York).`))
        : h("div", { class: "callout" }, t("Not locked. When a rule fires, every working order is cancelled, every position closed at market and the account locked until the next local day."));
      const unlockBtn = lock ? h("button", { type: "button", class: "btn btn-ghost btn-danger", onClick: async () => {
        if (!(await confirmDialog({ title: t("Unlock {spec}?", { spec: maskAccount(a.spec) }), body: t("Bridge orders are accepted again today. The rules stay in place and can fire again."), confirmText: t("Unlock"), danger: true }))) return;
        try { await api.post("/api/risk/unlock", { spec: a.spec }); toast("Account unlocked", "success"); closeDrawer(); actions.loadTradeAccounts(); }
        catch (e) { toast(e.message, "error"); }
      } }, icon("key"), t("Unlock for today")) : null;
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
      } }, icon("check"), t("Save rules"));
      openDrawer({
        title: t("Risk guard · {spec}", { spec: maskAccount(a.spec) }),
        body: [
          lockBox,
          h("div", { class: "field" }, h("label", null, t("Daily loss limit")), lossInp, h("div", { class: "field-hint" }, t("Account currency. Fires when today's P&L (realised + open, the broker's figures) reaches −limit. 0 = off."))),
          h("div", { class: "field" }, h("label", null, t("Daily profit target")), profitInp, h("div", { class: "field-hint" }, t("Fires when today's P&L reaches +target — locks in the day. 0 = off."))),
          h("div", { class: "field" }, h("label", null, t("Flatten at")), h("div", { style: "display:flex;gap:8px;flex-wrap:wrap" }, timeInp, tzSel), h("div", { class: "field-hint" }, t("Everything on this account is closed at that time and the account is locked for the rest of the trading day. New York time follows the exchange through the daylight-saving weeks; the journal timezone is your own clock. Empty = off."))),
          h("p", { class: "hint" }, t("Checked on every live P&L poll (Settings → General → Live P&L refresh, at least every few seconds while a rule is set). Applies to every path that trades this account: webhooks, Discord signals, marketplace subscriptions and copy trading.")),
        ],
        foot: [saveRisk, h("button", { type: "button", class: "btn btn-ghost", onClick: () => closeDrawer() }, t("Close")), h("span", { style: "flex:1" }), unlockBtn],
      });
    }
    const discovered = dataTable({
      empty: t("No accounts yet — add a login above, save, then Connect & Verify."),
      columns: [
        { label: t("Login"), render: (a) => a.token_name || "—" },
        { label: t("Account"), render: (a) => h("code", null, maskAccount(a.spec) || "—") },
        { label: t("Env"), render: (a) => tag((a.environment || "—").toUpperCase(), a.environment === "live" ? "live" : "demo") },
        { label: t("Login enabled"), render: (a) => h("span", { class: a.token_enabled ? "pos" : "muted" }, a.token_enabled ? "yes" : "no") },
        { label: t("Status"), render: (a) => h("span", { class: a.connected ? "pos" : "neg" }, a.connected ? t("Connected") : t("Not connected")) },
        { label: t("Risk guard"), render: (a) => h("span", { class: "inline-actions" },
          a.locked ? tag("locked", "warn") : null,
          h("button", { type: "button", class: "btn btn-ghost btn-sm", title: t("Daily loss / profit limit, flatten time"), onClick: () => riskDrawer(a) }, icon("shield"), riskSummary(a) || "Set rules")) },
      ],
    });

    root.append(
      pageHead(t("Broker Accounts"), t("One row per broker login — Tradovate (access token, auto-renewed), Rithmic or ProjectX (credentials). Every signal fans out to the accounts a webhook routes to, in parallel."), [
        h("button", { type: "button", class: "btn", onClick: () => { tbody.append(row()); markDirty(); } }, icon("plus"), t("Add login")),
      ]),
      card({ title: t("Logins"), hint: t("Pick the broker per row. Tradovate: paste the access token (and optionally the check token) from the web trader — see Tools for the extractor. Rithmic: user, password, system name and gateway. ProjectX: user name, API key and firm. Masked secrets keep their stored value when you save.") },
        h("div", { class: "table-scroll" }, h("table", { class: "data-table" },
          h("thead", null, h("tr", null, h("th", null, t("On")), h("th", null, t("Name")), h("th", null, t("Broker")), h("th", null, t("Env")), h("th", { colspan: 2 }, t("Credentials")), h("th", null, t("Qty ×")), h("th", null, t("Execute via")), h("th", null, t("Token expires")), h("th"))), tbody),
          h("datalist", { id: "rithmic-systems" }, ["Rithmic Paper Trading", "Rithmic Test", "Rithmic 01", "Apex", "TopstepTrader", "MyFundedFutures", "Bulenox", "Earn2Trade", "TradeFundrr"].map((x) => h("option", { value: x }))),
          h("datalist", { id: "rithmic-gateways" }, ["chicago", "europe", "paper", "test"].map((x) => h("option", { value: x }))),
          h("datalist", { id: "px-firms" }, ["topstep", "alphaticks", "bulenox", "blusky", "e8x", "fundingfutures", "thefuturesdesk", "futureselite", "fxifyfutures", "goatfundedfutures", "tickticktrader", "toponefutures", "tradeify", "daytraders", "lucidtrading", "holaprime", "nexgen", "aquafutures", "demo"].map((x) => h("option", { value: x })))),
        h("div", { class: "form-actions", style: "margin-top:12px" }, saveBtn, connectBtn, saveHint)),
      card({ title: t("Discovered trade accounts"), hint: t("Every trade account found under your logins — one login can hold several. Which accounts a signal actually trades is chosen per webhook (Webhooks → Accounts tab).") }, discovered.el),
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
