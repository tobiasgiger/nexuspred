/* Logs: event log + signal log, filterable, live via the stream. */
import { h, card, fmtTime, pageHead, clear } from "../ui.js";
import { icon } from "../icons.js";
import { store } from "../store.js";
import { actions } from "../actions.js";

function eventLine(e) {
  return h("div", { class: "log-line" },
    h("span", { class: "lt" }, fmtTime(e.ts)),
    h("span", { class: `lv ${e.level || ""}` }, (e.level || "").toUpperCase()),
    h("span", { class: "msg" }, e.message || ""));
}

function signalLine(s) {
  const r = String(s.result || "");
  const tone = r.startsWith("error") ? "error" : r === "skipped" ? "warn" : "info";
  return h("div", { class: "log-line" },
    h("span", { class: "lt" }, fmtTime(s.ts)),
    h("span", { class: `lv ${tone}` }, r),
    h("code", null, JSON.stringify(s.payload)));
}

export default {
  title: "Logs",
  render(root) {
    let level = "all";
    let q = "";
    const eventBox = h("div", { class: "log-stream" });
    const signalBox = h("div", { class: "log-stream" });
    const search = h("input", { type: "search", placeholder: "Filter events…", class: "input-sm", onInput: (e) => { q = e.target.value.toLowerCase(); paintEvents(); } });
    const chips = ["all", "info", "warn", "error"].map((l) => h("button", { type: "button", class: `chip ${l === level ? "active" : ""}`, onClick: (e) => {
      level = l; chips.forEach((c) => c.classList.toggle("active", c === e.currentTarget)); paintEvents();
    } }, l));

    function paintEvents() {
      const list = (store.get("events") || []).filter((e) => (level === "all" || e.level === level) && (!q || String(e.message || "").toLowerCase().includes(q)));
      clear(eventBox);
      if (!list.length) eventBox.append(h("div", { class: "empty-state" }, "No events"));
      else eventBox.append(...list.map(eventLine));
    }
    function paintSignals() {
      const list = store.get("signals") || [];
      clear(signalBox);
      if (!list.length) signalBox.append(h("div", { class: "empty-state" }, "No signals yet"));
      else signalBox.append(...list.map(signalLine));
    }

    root.append(
      pageHead("Logs", "Every event and every received signal, newest first. Updates arrive live; Refresh re-syncs from the server.", [
        h("button", { class: "btn", onClick: () => actions.refreshLogs() }, icon("refresh"), "Refresh"),
      ]),
      card({ title: "Event log" }, h("div", { class: "log-toolbar" }, h("div", { class: "chips" }, chips), search), eventBox),
      card({ title: "Received signals", hint: "Result column: received → executed status (ok / skipped / error)." }, signalBox),
    );
    const unsubs = [
      store.subscribe("events", paintEvents, { immediate: true }),
      store.subscribe("signals", paintSignals, { immediate: true }),
    ];
    return () => unsubs.forEach((u) => u());
  },
};
