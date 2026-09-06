/* Discord monitoring: listener status + live signal feed. */
import { h, card, tag, fmtTime, pageHead, clear } from "../ui.js";
import { icon } from "../icons.js";
import { store } from "../store.js";
import { actions } from "../actions.js";

const STATE_LABEL = { connected: "Connected", connecting: "Connecting…", disabled: "Disabled", error: "Error",
  library_missing: "Library missing", stopped: "Stopped", not_entitled: "Not enabled", token_invalid: "Token rejected" };

function feedLine(ev) {
  const time = h("span", { class: "lt" }, fmtTime(ev.ts));
  if (ev.kind === "unrecognized") {
    return h("div", { class: "log-line" }, time, h("span", { class: "lv warn" }, "UNKNOWN"),
      h("code", null, `${ev.channel_label || ""} — ${(ev.raw && ev.raw.title) || ""}`));
  }
  const sig = ev.signal || {};
  const targets = ev.targets || [];
  const ok = targets.filter((t) => t.ok).length;
  const failed = targets.filter((t) => t.ok === false).length;
  const targetTxt = ev.dry_run ? `${targets.length} target(s) skipped (dry-run)` : `${ok}/${targets.length} sent${failed ? `, ${failed} failed` : ""}`;
  const lat = ev.latency_ms != null ? ` · ${ev.latency_ms} ms` : "";
  return h("div", { class: "log-line" }, time,
    h("span", { class: "lv info" }, sig.event_type || "signal"),
    h("code", null, `${ev.channel_label || ""} · ${sig.symbol || "?"}${sig.side ? " " + sig.side : ""}`),
    ev.source ? tag(ev.source) : null, ev.dry_run ? tag("DRY", "sim") : null,
    h("span", { class: "muted" }, `— ${targetTxt}${lat}`));
}

export default {
  title: "Discord",
  gate: "discord",
  render(root, { navigate }) {
    const stateV = h("div", { class: "v" }, h("span", null, "—"));
    const dryV = h("div", { class: "v" }, h("span", null, "—"));
    const watchedV = h("div", { class: "v" }, h("span", null, "—"));
    const lastV = h("div", { class: "v" }, h("span", null, "—"));
    const libWarn = h("div", { class: "callout warn hidden" }, h("strong", null, "Listener library not installed. "),
      "The Discord listener needs ", h("code", null, "discord.py-self"), ". Parsing, config and the test button work without it, but no live channel is watched until it's installed and the process restarts.");
    const feed = h("div", { class: "log-stream" });
    const streamDot = h("span", { class: "dot off" });

    root.append(
      pageHead("Discord", "Signals parsed from the watched Discord channels and where they were forwarded, in real time.", [
        h("button", { class: "btn", onClick: () => navigate("/settings/discord") }, icon("settings"), "Configure"),
      ]),
      h("div", { class: "kpis" },
        h("div", { class: "kpi" }, h("div", { class: "k" }, icon("discord"), "Listener"), stateV),
        h("div", { class: "kpi" }, h("div", { class: "k" }, icon("shield"), "Dry-run"), dryV),
        h("div", { class: "kpi" }, h("div", { class: "k" }, icon("inbox"), "Watched channels"), watchedV),
        h("div", { class: "kpi" }, h("div", { class: "k" }, icon("clock"), "Last signal"), lastV)),
      libWarn,
      card({ title: ["Live signal feed", h("span", { class: "pill" }, streamDot, h("span", { class: "pill-text" }, "stream"))],
        hint: "Unrecognised messages are shown too, so a change in a provider's format is never silently lost." }, feed),
    );

    function paintFeed(events) {
      clear(feed);
      if (!events || !events.length) feed.append(h("div", { class: "empty-state" }, "No signals yet."));
      else feed.append(...events.map(feedLine));
    }
    const unsubs = [
      store.subscribe("discordStatus", (s) => {
        if (!s) return;
        const down = s.health === "down";
        const label = STATE_LABEL[s.state] || s.state;
        stateV.className = "v " + (s.state === "connected" ? "on" : (down || s.state === "error" || s.state === "token_invalid") ? "off" : "");
        stateV.firstChild.textContent = (s.enabled ? label : "Off") + (s.user ? ` (${s.user})` : "");
        dryV.className = "v " + (s.dry_run ? "warn" : "");
        dryV.firstChild.textContent = s.dry_run ? "ON" : "off";
        watchedV.firstChild.textContent = String((s.watched_channels || []).length);
        lastV.firstChild.textContent = s.last_event_ts ? `${fmtTime(s.last_event_ts)} · ${s.last_event_channel || ""}` : "—";
        libWarn.classList.toggle("hidden", !!s.library_available);
      }, { immediate: true }),
      store.subscribe("discordFeed", paintFeed, { immediate: true }),
      store.subscribe("stream", (s) => { streamDot.className = "dot " + (s === "live" ? "on" : s === "reconnecting" ? "warn" : "off"); }, { immediate: true }),
    ];
    actions.refreshDiscordStatus();
    if (!store.get("discordFeed").length) actions.loadDiscordFeed();
    return () => unsubs.forEach((u) => u());
  },
};
