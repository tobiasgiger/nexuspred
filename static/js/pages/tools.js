/* Tools: webhook URL + alert message, test signal, Discord test embed,
   browser token extractor, bookmarklets. */
import { h, card, toast, copyButton, copyText, pageHead } from "../ui.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { store, can } from "../store.js";
import { actions } from "../actions.js";
import { alertMessageTemplate, PRESETS, DS_TEST_PRESETS, BOOKMARKLETS, webhookUrl } from "../templates.js";

export default {
  title: "Tools",
  render(root, { navigate }) {
    const me = store.get("me");

    // ---- Webhook URL + template
    const sel = h("select");
    const urlCode = h("code", null, "—");
    const tmplPre = h("pre", { class: "code" }, "—");
    const tmplHint = h("p", { class: "hint" });
    const selected = () => (store.get("webhooks") || []).find((w) => w.id === sel.value) || null;
    function paintSelect(list) {
      const prev = sel.value;
      sel.replaceChildren();
      if (!list.length) {
        sel.append(h("option", { value: "" }, "No webhooks — create one first"));
      } else {
        sel.append(...list.map((w) => h("option", { value: w.id }, `${w.name} (${w.strategy})`)));
        sel.value = list.some((w) => w.id === prev) ? prev : list[0].id;
      }
      paintTemplate();
    }
    function paintTemplate() {
      const w = selected();
      urlCode.textContent = w ? webhookUrl(w.token) : "—";
      if (!w) { tmplPre.textContent = "—"; tmplHint.textContent = ""; return; }
      const t = alertMessageTemplate(w.strategy);
      tmplPre.textContent = t.json;
      tmplHint.textContent = t.hint;
    }
    sel.addEventListener("change", paintTemplate);

    // ---- Test signal
    const payloadTa = h("textarea", { rows: 10, spellcheck: "false" }, JSON.stringify(PRESETS.simple_buy.payload, null, 2));
    const testResult = h("pre", { class: "result-box hidden" });
    const sendBtn = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
      const w = selected();
      if (!w) return toast("Create a webhook first", "error");
      let payload;
      try { payload = JSON.parse(payloadTa.value); } catch { return toast("Payload is not valid JSON", "error"); }
      sendBtn.disabled = true;
      try {
        const r = await api.post(`/api/webhooks/${w.id}/test`, payload);
        testResult.textContent = JSON.stringify(r, null, 2); testResult.classList.remove("hidden");
        toast("Signal processed", "success");
        actions.refreshOrders(); actions.refreshLogs(); actions.refreshStatus();
      } catch (e) {
        testResult.textContent = "Error: " + e.message; testResult.classList.remove("hidden");
        toast(e.message, "error");
      } finally { sendBtn.disabled = false; }
    } }, icon("send"), "Send test signal");

    // ---- Discord test
    let dsCard = null;
    if (can(me, "discord")) {
      const chanInp = h("input", { placeholder: "e.g. 123456789012345678" });
      const presetSel = h("select", null, Object.entries(DS_TEST_PRESETS).map(([k, p]) => h("option", { value: k }, p.label)));
      const embedTa = h("textarea", { rows: 8, spellcheck: "false" }, JSON.stringify(DS_TEST_PRESETS.entry.embed, null, 2));
      presetSel.addEventListener("change", () => { embedTa.value = JSON.stringify(DS_TEST_PRESETS[presetSel.value].embed, null, 2); });
      const dsResult = h("pre", { class: "result-box" }, "—");
      const dsSend = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
        let embed;
        try { embed = JSON.parse(embedTa.value); } catch { return toast("Embed is not valid JSON", "error"); }
        const channel_id = chanInp.value.trim();
        if (!channel_id) return toast("Enter a channel ID", "error");
        try {
          const r = await api.post("/api/discord/test", { channel_id, embed, force: true });
          dsResult.textContent = JSON.stringify(r, null, 2);
          toast("Test embed processed", "success");
          actions.loadDiscordFeed();
        } catch (e) { dsResult.textContent = "Error: " + e.message; toast(e.message, "error"); }
      } }, icon("send"), "Send test embed");
      dsCard = card({ title: "Test Discord signal", hint: "Runs a synthetic Discord embed through the listener pipeline for a channel (parse → dispatch), respecting the dry-run switch — verify fan-out without a live Discord connection." },
        h("div", { class: "grid grid-2" },
          h("div", { class: "field" }, h("label", null, "Channel ID"), chanInp),
          h("div", { class: "field" }, h("label", null, "Preset"), presetSel)),
        h("div", { class: "field" }, h("label", null, "Embed (JSON: title + fields)"), embedTa),
        h("div", { class: "form-actions" }, dsSend), dsResult);
    }

    // ---- Bookmarklets
    const bm = (key, label) => h("div", { class: "bm-row" },
      h("a", { class: "btn btn-secondary bm-link", href: BOOKMARKLETS[key], draggable: "true", onClick: (e) => { e.preventDefault(); toast("Drag this button to your bookmarks bar, then click it on the site."); } }, "◈ " + label),
      copyButton(() => decodeURIComponent(BOOKMARKLETS[key]), "Copy code"));

    root.append(
      pageHead("Tools", "Everything you need to wire TradingView and Discord to the bridge, and to test it safely."),
      card({ title: "Webhook URL & alert message" },
        h("div", { class: "field", style: "max-width:420px" }, h("label", null, "Webhook"), sel),
        h("p", { class: "hint" }, "Point the TradingView alert's Webhook URL (POST, JSON body) at:"),
        h("div", { class: "url-box" }, urlCode, copyButton(() => urlCode.textContent)),
        h("h3", null, "Alert message"),
        h("p", { class: "hint" }, "Paste into the TradingView alert's \"Message\" box."),
        tmplPre, h("div", { class: "form-actions" }, copyButton(() => tmplPre.textContent, "Copy message", "btn btn-secondary btn-sm")), tmplHint,
        h("div", { class: "form-actions" }, h("button", { type: "button", class: "btn btn-ghost", onClick: () => navigate("/webhooks") }, "Manage webhooks ", icon("chevron")))),
      card({ title: "Send test signal", hint: "Runs the full live pipeline for the selected webhook — real execution if Trading is enabled. Load an example, then send." },
        h("div", { class: "preset-row" }, Object.entries(PRESETS).map(([k, p]) => h("button", { type: "button", class: "chip", onClick: () => { payloadTa.value = JSON.stringify(p.payload, null, 2); } }, p.label))),
        payloadTa, h("div", { class: "form-actions" }, sendBtn), testResult),
      dsCard,
      card({ title: "Browser token extractor (Chrome / Edge)", actions: [h("a", { class: "btn btn-primary", href: "/api/extension/token-extractor.zip", download: "" }, icon("download"), "Download extension (.zip)")] },
        h("p", { class: "hint" }, "A small browser extension that reads your own tokens from a logged-in tab — the Discord user token (Settings → Discord Listener) and the Tradovate token + checkToken (Settings → Broker Accounts). It runs 100% locally and never sends anything anywhere. Each token is like a password for that account — only paste it back into this bridge."),
        h("h3", null, "Install (one time)"),
        h("ol", { class: "steps" },
          h("li", null, h("span", { class: "step-num" }, "1"), h("div", null, h("strong", null, "Download"), " the .zip above and unzip it — you get a ", h("code", null, "token-extractor"), " folder.")),
          h("li", null, h("span", { class: "step-num" }, "2"), h("div", null, "Open ", h("code", null, "chrome://extensions"), " (or ", h("code", null, "edge://extensions"), ") and turn on ", h("strong", null, "Developer mode"), ".")),
          h("li", null, h("span", { class: "step-num" }, "3"), h("div", null, "Click ", h("strong", null, "Load unpacked"), " and select the unzipped folder."))),
        h("h3", null, "Use"),
        h("ol", { class: "steps" },
          h("li", null, h("span", { class: "step-num" }, "1"), h("div", null, "Open ", h("a", { href: "https://discord.com/app", target: "_blank", rel: "noopener" }, "discord.com"), " or the ", h("a", { href: "https://trader.tradovate.com", target: "_blank", rel: "noopener" }, "Tradovate web trader"), ", logged in.")),
          h("li", null, h("span", { class: "step-num" }, "2"), h("div", null, "Click the extension icon → ", h("strong", null, "Extract from active tab"), " → Reveal / Copy.")),
          h("li", null, h("span", { class: "step-num" }, "3"), h("div", null, "Paste into the matching field here and save."))),
        h("p", { class: "hint" }, "After a bridge update, re-download and reload the extension (↻ in chrome://extensions) to pick up changes.")),
      card({ title: "No-install option: bookmarklets", hint: "Drag a button to your bookmarks bar once, then click it while on the site (logged in) to copy the token. Discord's strict security policy can block bookmarklets — if the Discord one does nothing, use the extension. Tradovate works reliably." },
        bm("discord", "Discord token"), bm("tradovate", "Tradovate token + checkToken")),
    );

    const unsub = store.subscribe("webhooks", (list) => paintSelect(list || []), { immediate: true });
    return () => unsub();
  },
};
