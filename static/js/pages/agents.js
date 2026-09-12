/* Settings → Execution Agents: pair helper agents that run on a VPS and execute
   Tradovate calls for assigned logins, so each account trades from its own IP. */
import { h, card, tag, toast, confirmDialog, pageHead, fmtDateTime, copyText, clear } from "../ui.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { dataTable } from "../components/table.js";

export default {
  title: "Execution Agents",
  gate: "admin",
  render(root) {
    const nameInput = h("input", { class: "input-sm", placeholder: "e.g. VPS Frankfurt", style: "min-width:200px" });
    const codeBox = h("div", { class: "callout ok hidden" });
    async function newCode() {
      try {
        const r = await api.post("/api/agents/pairing-code", { name: nameInput.value.trim() });
        clear(codeBox);
        codeBox.append(
          h("div", null, "Pairing code for ", h("strong", null, r.name), " — valid ", String(Math.round(r.expires_in / 60)), " minutes, single use:"),
          h("div", { style: "display:flex;gap:10px;align-items:center;margin-top:8px;flex-wrap:wrap" },
            h("code", { style: "font-size:22px;letter-spacing:3px;padding:6px 12px" }, r.code),
            h("button", { type: "button", class: "btn btn-sm", onClick: async () => toast((await copyText(r.code)) ? "Code copied" : "Copy failed", "success") }, icon("copy"), "Copy")),
          h("div", { class: "muted", style: "margin-top:8px;font-size:12.5px" },
            "On the VPS run ", h("code", null, "start-agent.bat"), " (or ", h("code", null, "python fluxbridge_agent.py"), "), enter this bridge's URL ",
            h("code", null, window.location.origin), " and the code. The agent then appears below within a few seconds."));
        codeBox.classList.remove("hidden");
      } catch (e) { toast(e.message, "error"); }
    }

    async function linuxOneLiner() {
      try {
        const r = await api.post("/api/agents/pairing-code", { name: nameInput.value.trim() });
        const cmd = `curl -fsSL https://raw.githubusercontent.com/tobiasgiger/nexuspred/main/deploy/install-agent.sh | sudo bash -s -- --bridge ${window.location.origin} --code ${r.code} --name "${r.name.replace(/"/g, "")}"`;
        clear(codeBox);
        codeBox.append(
          h("div", null, "Linux / macOS: run this on the VPS as root — it installs Python if needed, pairs as ", h("strong", null, r.name), " and starts a service (boot + restart). Valid ", String(Math.round(r.expires_in / 60)), " minutes, single use:"),
          h("pre", { style: "margin-top:8px;white-space:pre-wrap;word-break:break-all;font-size:12.5px" }, cmd),
          h("div", { style: "display:flex;gap:10px;align-items:center;flex-wrap:wrap" },
            h("button", { type: "button", class: "btn btn-sm btn-primary", onClick: async () => toast((await copyText(cmd)) ? "Command copied" : "Copy failed", "success") }, icon("copy"), "Copy command"),
            h("span", { class: "muted", style: "font-size:12.5px" }, "Afterwards: ", h("code", null, "fluxbridge-agent status | logs -f | update | uninstall"))));
        codeBox.classList.remove("hidden");
      } catch (e) { toast(e.message, "error"); }
    }

    async function downloadPreconfigured() {
      const name = nameInput.value.trim() || "agent";
      dlBtn.disabled = true; dlBtn.textContent = "Preparing…";
      try {
        const res = await fetch("/api/agents/bundle", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
        if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || res.statusText); }
        const blob = await res.blob();
        const a = h("a", { href: URL.createObjectURL(blob), download: (res.headers.get("Content-Disposition") || "").match(/filename="([^"]+)"/)?.[1] || "fluxbridge-agent.zip" });
        document.body.append(a); a.click(); a.remove();
        const exe = res.headers.get("X-Agent-Exe") === "1";
        clear(codeBox);
        codeBox.append(
          h("div", null, "Agent ", h("strong", null, name), " is registered and its token is inside the zip. "),
          h("div", { class: "muted", style: "margin-top:6px;font-size:12.5px" },
            "Unzip on the VPS and start ", h("code", null, exe ? "fluxbridge-agent.exe" : "start-agent.bat"), exe ? " — no Python needed." : " (Python 3 required; the .exe build was not reachable right now).",
            " It appears below as online within seconds. Keep the zip private: it contains the agent's token."));
        codeBox.classList.remove("hidden");
        load();
      } catch (e) { toast(e.message, "error"); }
      finally { dlBtn.disabled = false; clear(dlBtn); dlBtn.append(icon("download"), "Download preconfigured agent"); }
    }
    const dlBtn = h("button", { type: "button", class: "btn btn-primary", onClick: downloadPreconfigured }, icon("download"), "Download preconfigured agent");

    const table = dataTable({ empty: "No agents paired yet. Create a pairing code, then start the agent on your VPS.", compact: true, columns: [
      { label: "Agent", render: (a) => [h("strong", null, a.name), " ", h("span", { class: "muted" }, `#${a.id}`)] },
      { label: "Status", render: (a) => a.online ? tag("online", "ok") : tag("offline", "error") },
      { label: "Last seen", render: (a) => a.last_seen_at ? fmtDateTime(a.last_seen_at) : "never" },
      { label: "IP", render: (a) => h("code", null, a.last_ip || "—") },
      { label: "Version", render: (a) => a.version || "—" },
      { label: "Queued", className: "num", render: (a) => String(a.pending_jobs || 0) },
      { label: "", render: (a) => h("div", { class: "inline-actions" },
        h("button", { type: "button", class: "btn btn-ghost btn-sm", title: "Rename", onClick: async () => {
          const name = window.prompt("New name for this agent", a.name);
          if (!name || name === a.name) return;
          try { await api.put(`/api/agents/${a.id}`, { name }); load(); } catch (e) { toast(e.message, "error"); }
        } }, icon("edit")),
        h("button", { type: "button", class: "btn btn-ghost btn-sm", title: "Revoke", onClick: async () => {
          if (!(await confirmDialog({ title: `Revoke agent "${a.name}"?`, body: "Its token stops working immediately. Logins assigned to it will fail until you re-assign them.", confirmText: "Revoke", danger: true }))) return;
          try { await api.del(`/api/agents/${a.id}`); toast("Agent revoked", "success"); load(); } catch (e) { toast(e.message, "error"); }
        } }, icon("trash"))) },
    ] });

    let timer = null;
    async function load() {
      try { table.update(await api.get("/api/agents")); } catch (e) { /* ignore */ }
    }

    root.append(
      pageHead("Execution Agents", "Run a small helper on a VPS and route a Tradovate login's calls through it, so each account trades from its own IP (Tradovate logins only — Rithmic and ProjectX connect from the bridge). The agent pairs with a one-time code and never sees your dashboard login.", [
        h("a", { class: "btn btn-ghost", href: "/api/agents/download.zip", title: "Plain agent files without token (pair with a code)" }, icon("download"), "Plain agent (no token)"),
      ]),
      card({ title: "Add an agent", hint: "Windows VPS: name it and download the preconfigured agent — the zip already contains the bridge URL, this agent's token and the .exe. Linux / macOS VPS: name it and press Linux one-liner — one command installs, pairs and starts the agent as a service. Then assign logins under Broker Accounts → Execute via. Alternative: a pairing code, typed into the plain agent on first start." },
        h("div", { class: "form-actions" }, nameInput, dlBtn,
          h("button", { type: "button", class: "btn", onClick: linuxOneLiner, title: "One command that installs, pairs and starts the agent as a service" }, icon("terminal"), "Linux one-liner"),
          h("button", { type: "button", class: "btn", onClick: newCode }, icon("key"), "New pairing code")),
        codeBox),
      card({ title: "Paired agents", hint: "Online = polled the bridge within the last 45 seconds. Everything an assigned login does with Tradovate (orders, token renewal, health checks, P&L) goes through its agent; if the agent is offline those calls fail loudly instead of using the bridge's IP.", actions: [
        h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: load }, icon("refresh"), "Refresh"),
      ] }, table.el),
    );
    load();
    timer = setInterval(load, 10000);
    return () => { if (timer) clearInterval(timer); };
  },
};
