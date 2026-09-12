/* Trade simulator: run scenarios through the real signal logic, in memory. */
import { h, card, tag, toast, pageHead, clear } from "../ui.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { dataTable } from "../components/table.js";
import { t } from "../i18n.js";

export default {
  title: t("Simulator"),
  render(root) {
    let scenarios = [];
    let simIndex = 0;
    let running = false;
    const sel = h("select");
    const desc = h("p", { class: "sim-desc" });
    const progress = h("span", { class: "sim-progress" });
    const steps = h("ol", { class: "sim-steps" });
    const positions = dataTable({ empty: t("Flat"), columns: [
      { label: t("Symbol"), render: (p) => p.symbol },
      { label: t("Net"), className: "num", render: (p) => h("span", { class: p.netPos >= 0 ? "pos" : "neg" }, String(p.netPos)) },
      { label: t("Avg price"), className: "num", render: (p) => String(p.netPrice ?? "—") },
    ] });
    const working = dataTable({ empty: t("None"), columns: [
      { label: "ID", render: (o) => String(o.id) },
      { label: t("Side"), render: (o) => tag(o.action, (o.action || "").toLowerCase()) },
      { label: t("Qty"), className: "num", render: (o) => String(o.qty) },
      { label: t("Type"), render: (o) => o.order_type },
      { label: t("Price"), className: "num", render: (o) => String(o.price ?? o.stop_price ?? "—") },
    ] });

    const current = () => scenarios[Number(sel.value) || 0];

    async function refreshState() {
      try {
        const st = await api.get("/api/simulate/state");
        positions.update(st.positions || []);
        working.update(st.working_orders || []);
      } catch (e) { /* ignore */ }
    }

    function paintProgress() {
      const sc = current();
      progress.textContent = sc ? t("{i} / {n} executed", { i: simIndex, n: sc.steps.length }) : "";
      steps.querySelectorAll(".sim-step").forEach((el, i) => el.classList.toggle("current", i === simIndex));
    }

    function paintScenario() {
      const sc = current();
      if (!sc) return;
      simIndex = 0;
      desc.textContent = sc.description || "";
      clear(steps);
      steps.append(...sc.steps.map((step, i) => {
        const body = h("div", { class: "sim-step-body" }, h("pre", { class: "sim-signal" }, JSON.stringify(step.signal, null, 2)), h("pre", { class: "sim-result hidden" }));
        const head = h("div", { class: "sim-step-head" }, h("span", { class: "sim-step-num" }, String(i + 1)), h("span", { class: "sim-step-label" }, step.label), h("span", { class: "sim-step-status" }));
        const li = h("li", { class: "sim-step", dataset: { i: String(i) } }, head, body);
        head.addEventListener("click", () => li.classList.toggle("open"));
        return li;
      }));
      paintProgress();
      refreshState();
    }

    async function runStep(i) {
      const sc = current();
      if (!sc || i >= sc.steps.length) return false;
      const step = sc.steps[i];
      const el = steps.querySelector(`.sim-step[data-i="${i}"]`);
      const statusEl = el.querySelector(".sim-step-status");
      const resultEl = el.querySelector(".sim-result");
      statusEl.textContent = t("running…");
      try {
        const r = await api.post("/api/simulate", step.signal);
        el.classList.remove("failed"); el.classList.add("done");
        const n = (r.orders || []).length;
        statusEl.textContent = r.status === "ok" ? (n ? `✓ ${n} order(s)` : "✓ " + (r.action || "ok")) : "• " + (r.reason || r.status);
        resultEl.textContent = JSON.stringify(r, null, 2); resultEl.classList.remove("hidden");
        return true;
      } catch (e) {
        el.classList.add("failed");
        statusEl.textContent = "✗ " + e.message;
        resultEl.textContent = t("Error: ") + e.message; resultEl.classList.remove("hidden");
        return false;
      } finally { refreshState(); }
    }

    const runAll = h("button", { class: "btn btn-primary", onClick: async () => {
      const sc = current(); if (!sc || running) return;
      running = true; runAll.disabled = true;
      for (; simIndex < sc.steps.length; simIndex++) {
        paintProgress();
        if (!(await runStep(simIndex))) break;
        await new Promise((r) => setTimeout(r, 600));
      }
      paintProgress(); running = false; runAll.disabled = false;
      toast("Simulation finished", "success");
    } }, icon("play"), t("Run all"));
    const stepBtn = h("button", { class: "btn btn-secondary", onClick: async () => {
      const sc = current();
      if (!sc || simIndex >= sc.steps.length) return toast("Scenario complete — reset to run again");
      if (await runStep(simIndex)) { simIndex++; paintProgress(); }
    } }, icon("skip"), t("Run next step"));
    const resetBtn = h("button", { class: "btn btn-ghost", onClick: async () => {
      try { await api.post("/api/simulate/reset"); } catch (e) { /* ignore */ }
      paintScenario(); toast("Simulation reset");
    } }, icon("refresh"), t("Reset"));
    sel.addEventListener("change", async () => { try { await api.post("/api/simulate/reset"); } catch (e) { /* ignore */ } paintScenario(); });

    root.append(
      pageHead(t("Simulator"), t("Rehearse a complete trade lifecycle through the real signal logic — entries, brackets, stop moves, partial closes — without sending anything to a broker. No credentials needed.")),
      card({ title: t("Scenario") },
        h("div", { class: "sim-controls" }, h("div", { class: "field" }, h("label", null, t("Scenario")), sel), h("div", { class: "sim-buttons" }, runAll, stepBtn, resetBtn)),
        desc),
      h("div", { class: "grid grid-2" },
        card({ title: [t("Steps"), progress] }, steps),
        card({ title: t("Simulated account"), actions: [h("button", { class: "btn btn-ghost btn-sm", onClick: refreshState }, icon("refresh"), t("Refresh"))] },
          h("h3", null, t("Positions")), positions.el, h("h3", null, t("Working orders")), working.el)),
    );

    api.get("/api/scenarios").then((list) => {
      scenarios = list || [];
      sel.replaceChildren(...scenarios.map((s, i) => h("option", { value: String(i) }, s.name)));
      paintScenario();
    }).catch(() => toast("Could not load scenarios", "error"));
    return () => {};
  },
};
