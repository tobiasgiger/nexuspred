/* Settings → Discord Listener: self-bot token, dry-run, channels and their targets. */
import { h, card, toast, confirmDialog, pageHead, clear } from "../ui.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { store } from "../store.js";
import { actions } from "../actions.js";
import { t } from "../i18n.js";

export default {
  title: t("Discord Listener"),
  gate: "discord",
  render(root, { navigate }) {
    const enabled = h("input", { type: "checkbox", class: "switch" });
    const dryRun = h("input", { type: "checkbox", class: "switch" });
    const token = h("input", { type: "password", placeholder: t("your personal Discord user token"), autocomplete: "off" });
    const tokenEye = h("button", { type: "button", class: "btn btn-ghost btn-icon", title: t("Show / hide"), onClick: () => { const show = token.type === "password"; token.type = show ? "text" : "password"; tokenEye.replaceChildren(icon(show ? "eyeOff" : "eye")); } }, icon("eye"));
    const channels = h("div");
    let dirty = false;
    const markDirty = () => { dirty = true; hint.textContent = t("Unsaved changes"); hint.className = "save-hint"; };

    const webhookOptions = (selectedId) => [
      h("option", { value: "", selected: !selectedId }, t("Custom URL…")),
      ...(store.get("webhooks") || []).map((w) => h("option", { value: w.id, selected: w.id === selectedId }, `${w.name} (${w.strategy})`)),
    ];

    function targetRow(tg = {}) {
      const wid = tg.webhook_id || "";
      const sel = h("select", { class: "t-webhook input-sm", style: "min-width:200px" }, webhookOptions(wid));
      const url = h("input", { class: "t-url input-sm", value: tg.url || "", placeholder: "https://…/webhook/<token> or external URL" });
      const secret = h("input", { type: "password", class: "t-secret input-sm", value: tg.secret || "", placeholder: t("X-Webhook-Secret (optional)"), autocomplete: "off" });
      const custom = h("div", { class: `ds-target-custom ${wid ? "hidden" : ""}` }, url, secret);
      sel.addEventListener("change", () => custom.classList.toggle("hidden", sel.value !== ""));
      const tr = h("tr", { class: "ds-target" },
        h("td", null, h("input", { type: "checkbox", class: "switch t-enabled", checked: tg.enabled !== false })),
        h("td", null, h("input", { class: "t-label input-sm", value: tg.label || "", placeholder: t("(optional)"), style: "min-width:110px" })),
        h("td", null, sel, custom),
        h("td", { style: "width:44px" }, h("button", { type: "button", class: "btn btn-ghost btn-icon", title: t("Remove target"), onClick: () => { tr.remove(); markDirty(); } }, icon("trash"))));
      return tr;
    }

    function channelCard(c = {}) {
      const tbody = h("tbody", null, (c.targets || []).map(targetRow));
      const label = h("input", { class: "c-label input-sm", value: c.label || "", placeholder: t("Signal channel") });
      const id = h("input", { class: "c-id input-sm", value: c.id || "", placeholder: "123456789012345678", inputmode: "numeric" });
      const on = h("input", { type: "checkbox", class: "switch c-enabled", checked: c.enabled !== false });
      const el = h("div", { class: "card ds-channel" },
        h("div", { class: "card-head" },
          h("h2", null, h("label", { class: "switch-row", style: "padding:0;border:0;gap:10px" }, on, h("span", null, t("Channel")))),
          h("div", { class: "actions" }, h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: async () => {
            if (!(await confirmDialog({ title: t("Remove channel?"), body: t("Its targets are removed too (after you save)."), confirmText: t("Remove"), danger: true }))) return;
            el.remove(); markDirty();
          } }, icon("trash"), t("Remove")))),
        h("div", { class: "grid grid-2" },
          h("div", { class: "field" }, h("label", null, t("Label")), label),
          h("div", { class: "field" }, h("label", null, t("Channel ID")), id, h("div", { class: "field-hint" }, t("Discord → right-click the channel → Copy Channel ID (Developer Mode). No server ID needed.")))),
        h("h3", null, t("Targets")),
        h("div", { class: "table-scroll" }, h("table", { class: "data-table" }, h("thead", null, h("tr", null, h("th", null, t("On")), h("th", null, t("Label")), h("th", null, t("Target")), h("th"))), tbody)),
        h("div", { class: "form-actions", style: "margin-top:8px" }, h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: () => { tbody.append(targetRow({ enabled: true })); markDirty(); } }, icon("plus"), t("Add target"))));
      el.addEventListener("input", markDirty);
      el.addEventListener("change", markDirty);
      return el;
    }

    function paint(cfg) {
      enabled.checked = !!cfg.discord_enabled;
      dryRun.checked = !!cfg.discord_dry_run;
      token.value = cfg.discord_user_token || "";
      clear(channels);
      const list = cfg.discord_channels || [];
      if (!list.length) channels.append(h("div", { class: "empty-state" }, t("No channels yet — add one.")));
      else channels.append(...list.map(channelCard));
      dirty = false; hint.textContent = ""; hint.className = "save-hint";
    }

    function collect() {
      const out = [];
      for (const el of channels.querySelectorAll(".ds-channel")) {
        const targets = [...el.querySelectorAll(".ds-target")].map((r) => {
          const label = r.querySelector(".t-label").value.trim();
          const on = r.querySelector(".t-enabled").checked;
          const wid = r.querySelector(".t-webhook").value;
          if (wid) return { label, webhook_id: wid, enabled: on };
          return { label, enabled: on, url: r.querySelector(".t-url").value.trim(), secret: r.querySelector(".t-secret").value };
        }).filter((x) => x.webhook_id || x.url);
        out.push({ id: el.querySelector(".c-id").value.trim(), label: el.querySelector(".c-label").value.trim(), enabled: el.querySelector(".c-enabled").checked, targets });
      }
      return out.filter((c) => c.id);
    }

    const hint = h("span", { class: "save-hint" });
    const saveBtn = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
      try {
        const cfg = await api.post("/api/discord/config", { discord_enabled: enabled.checked, discord_dry_run: dryRun.checked, discord_user_token: token.value, discord_channels: collect() });
        store.set("discordConfig", cfg);
        paint(cfg);
        toast("Discord settings saved", "success");
        actions.refreshDiscordStatus();
      } catch (e) { hint.textContent = e.message; hint.className = "save-hint err"; toast(e.message, "error"); }
    } }, icon("check"), t("Save Discord settings"));

    root.append(
      pageHead(t("Discord Listener"), t("Watches Discord channels over the Gateway with your personal user token (self-bot) and forwards parsed signals to webhook targets. The live feed is on the Discord page."), [
        h("button", { type: "button", class: "btn", onClick: () => navigate("/discord") }, icon("discord"), t("Live feed")),
      ]),
      card({ title: t("Listener") },
        h("label", { class: "switch-row" }, h("span", null, t("Enable listener"), h("small", null, t("Connects to the Discord Gateway with the token below."))), enabled),
        h("label", { class: "switch-row" }, h("span", null, t("Global dry-run"), h("small", null, t("Parse and display only — send to no webhook."))), dryRun),
        h("div", { class: "field", style: "margin-top:14px" }, h("label", null, t("Discord user token (self-bot)")), h("div", { style: "display:flex;gap:6px;align-items:center" }, token, tokenEye),
          h("div", { class: "field-hint" }, t("Logs in as a normal client using your personal token. Leave the masked value to keep the stored token. Get it with the extractor under Tools.")))),
      h("div", { class: "page-head", style: "margin-top:4px" }, h("div", null, h("h1", { style: "font-size:15px" }, t("Channels")), h("p", { class: "lead" }, t("Each channel = a Discord channel ID with one or more targets. Every enabled target receives each signal in parallel; bridge webhooks are dispatched in-process, custom URLs get the secret as X-Webhook-Secret."))),
        h("div", { class: "actions" }, h("button", { type: "button", class: "btn", onClick: () => { const emptyEl = channels.querySelector(".empty-state"); if (emptyEl) emptyEl.remove(); channels.append(channelCard({ enabled: true, targets: [] })); markDirty(); } }, icon("plus"), t("Add channel")))),
      channels,
      h("div", { class: "dirty-bar", style: "margin-top:12px" }, h("span", { class: "msg" }, hint), saveBtn),
    );
    [enabled, dryRun, token].forEach((el) => { el.addEventListener("input", markDirty); el.addEventListener("change", markDirty); });

    const unsub = store.subscribe("discordConfig", (cfg) => { if (cfg && !dirty) paint(cfg); }, { immediate: true });
    actions.loadDiscordConfig();
    return () => unsub();
  },
};
