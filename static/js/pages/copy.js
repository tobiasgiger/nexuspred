/* Copy Trading: a leader trade account mirrored onto follower accounts in
   real time. A group = leader + symbol filter + followers with sizing. The
   table shows the live feed / latency / pause state per group; a drawer edits
   the group and shows leader vs follower positions and the group's actions.
   Deep link: #/copy/<id>. */
import { h, card, tag, toast, confirmDialog, pageHead, clear, fmtDateTime, fmtTime } from "../ui.js";
import { maskAccount } from "../privacy.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { store } from "../store.js";
import { actions } from "../actions.js";
import { dataTable } from "../components/table.js";
import { openDrawer, closeDrawer } from "../components/drawer.js";

const accountKey = (idx, spec) => `${idx}::${spec}`;
const CONTRACT_RE = /^([A-Z]{1,4})[FGHJKMNQUVXZ]\d{1,2}$/;
/** MNQU6 → MNQ, MNQ1! → MNQ (same rule as the backend). */
const baseRoot = (name) => { const m = CONTRACT_RE.exec(String(name || "").toUpperCase()); return m ? m[1] : String(name || "").toUpperCase().replace("1!", "").trim(); };
/** Roots the bridge knows from Settings → Symbol Mapping (map keys + values + allowed roots). */
function knownRoots(settings) {
  const out = new Set();
  const map = (settings && settings.symbol_map) || {};
  for (const [k, v] of Object.entries(map)) { if (k) out.add(baseRoot(k)); if (v) out.add(baseRoot(v)); }
  for (const r of (settings && settings.allowed_symbols) || []) out.add(baseRoot(r));
  return [...out].filter(Boolean).sort();
}
const KIND_TONE = { mirror: "on", feed_up: "on", resumed: "on", reject: "off", feed_lost: "warn", paused: "warn", drift: "warn", flatten: "warn", ws_miss: "warn", filtered: "", skipped: "", ignored: "",
  order_mirror: "on", order_modify: "accent", order_cancel: "", order_done: "", order_skip: "", order_reject: "off", ws_up: "", ws_lost: "" };
const orderText = (o) => `${o.action} ${o.qty} ${o.type}${o.price != null ? ` @ ${o.price}` : ""}${o.stop != null ? ` stop ${o.stop}` : ""}${o.oco ? " · OCO" : ""}`;

function feedTag(g) {
  const s = g.status;
  if (!g.enabled) return tag("off", "off");
  if (!s) return tag("starting…", "");
  if (s.paused) return tag("paused", "warn");
  if (!s.feed_ok) return tag("feed lost", "off");
  if (s.throttled) return tag("live · throttled", "warn");
  if (s.feed === "websocket") return tag(s.ws_ok ? "socket + poll · live" : "poll · live (socket down)", "on");
  return tag(`poll${s.poll_interval > 1 ? ` ${s.poll_interval}s` : ""} · live`, "on");
}

function latencyText(s) {
  if (!s || s.latency_ms == null) return "—";
  return `${s.latency_ms} ms`;
}

function signed(n) { return n > 0 ? `+${n}` : String(n); }

/** Drawer to create / edit one group and drive it. */
function groupDrawer(group, { reload, onClose = null }) {
  let g = { ...group };
  const known = store.get("tradeAccounts") || [];
  const isNew = !g.leader || !g.leader.spec;

  // --- General
  const nameInp = h("input", { value: g.name || "", maxlength: 60 });
  const enabledSw = h("input", { type: "checkbox", class: "switch", checked: !!g.enabled });
  const leaderSel = h("select", null,
    h("option", { value: "", selected: isNew }, "— choose the leader account —"),
    known.map((a) => h("option", { value: accountKey(a.token_idx, a.spec), selected: !isNew && a.token_idx === g.leader.token_idx && a.spec === g.leader.spec },
      `${maskAccount(a.spec)} · ${a.token_name} · ${(a.environment || "").toUpperCase()}${a.agent_id ? " · via agent" : ""}`)));
  // --- Symbols: chips from the symbol mapping + free text for anything else
  const roots = knownRoots(store.get("settings"));
  const chosen = new Set((g.symbols || []).map(baseRoot));
  const allSw = h("input", { type: "checkbox", class: "switch", checked: !chosen.size });
  const chips = h("div", { class: "check-list cp-syms" }, roots.map((r) => h("label", null, h("input", { type: "checkbox", class: "cp-sym", value: r, checked: chosen.has(r) }), r)));
  const extraInp = h("input", { value: [...chosen].filter((r) => !roots.includes(r)).join(", "), placeholder: "other roots, e.g. CL, RTY" });
  const symBox = h("div", { class: chosen.size ? "" : "hidden" }, chips, h("div", { class: "field", style: "margin-top:6px" }, extraInp));
  allSw.addEventListener("change", () => symBox.classList.toggle("hidden", allSw.checked));
  const collectSymbols = () => allSw.checked ? [] : [...new Set([...[...chips.querySelectorAll(".cp-sym:checked")].map((c) => c.value),
    ...extraInp.value.split(/[,;\s]+/).map(baseRoot).filter(Boolean)])];
  const feedSel = h("select", null,
    h("option", { value: "auto", selected: (g.feed || "auto") === "auto" }, "Auto (WebSocket, poll for agent logins)"),
    h("option", { value: "websocket", selected: g.feed === "websocket" }, "WebSocket (user sync, ~100 ms)"),
    h("option", { value: "poll", selected: g.feed === "poll" }, "Poll every second"));
  const lossInp = h("input", { type: "number", min: 5, max: 600, value: g.feed_loss_flatten_s ?? 30, style: "width:120px" });
  const addsSw = h("input", { type: "checkbox", class: "switch", checked: g.copy_adds !== false });
  const ordersSw = h("input", { type: "checkbox", class: "switch", checked: !!g.copy_orders });

  // --- Followers
  const selected = new Map((g.followers || []).map((f) => [accountKey(f.token_idx, f.spec), f]));
  const fTable = dataTable({
    compact: true,
    empty: "No trade accounts discovered yet — add a login under Settings → Tradovate Accounts and Connect & Verify.",
    columns: [
      { label: "Follow", render: (a) => h("input", { type: "checkbox", class: "switch cp-on", checked: !!(selected.get(accountKey(a.token_idx, a.spec)) || {}).enabled && selected.has(accountKey(a.token_idx, a.spec)), dataset: { key: accountKey(a.token_idx, a.spec) } }) },
      { label: "Account", render: (a) => h("span", null, h("code", null, maskAccount(a.spec)), h("small", { class: "muted", style: "display:block" }, `${a.token_name} · ${(a.environment || "").toUpperCase()}`)) },
      { label: "Mode", render: (a) => { const f = selected.get(accountKey(a.token_idx, a.spec)) || {}; return h("select", { class: "cp-mode input-sm", dataset: { key: accountKey(a.token_idx, a.spec) } },
        h("option", { value: "multiplier", selected: (f.mode || "multiplier") === "multiplier" }, "Multiplier"), h("option", { value: "fixed", selected: f.mode === "fixed" }, "Fixed")); } },
      { label: "×", render: (a) => h("input", { type: "number", class: "cp-mult input-sm", min: 0.01, step: 0.01, style: "width:70px", value: (selected.get(accountKey(a.token_idx, a.spec)) || {}).multiplier ?? 1, dataset: { key: accountKey(a.token_idx, a.spec) } }) },
      { label: "Fixed", render: (a) => h("input", { type: "number", class: "cp-fixed input-sm", min: 1, step: 1, style: "width:64px", value: (selected.get(accountKey(a.token_idx, a.spec)) || {}).fixed ?? 1, dataset: { key: accountKey(a.token_idx, a.spec) } }) },
      { label: "Max", render: (a) => h("input", { type: "number", class: "cp-max input-sm", min: 0, step: 1, style: "width:64px", title: "0 = no cap", value: (selected.get(accountKey(a.token_idx, a.spec)) || {}).max_contracts ?? 0, dataset: { key: accountKey(a.token_idx, a.spec) } }) },
      { label: "Direction", render: (a) => { const f = selected.get(accountKey(a.token_idx, a.spec)) || {}; return h("select", { class: "cp-dir input-sm", dataset: { key: accountKey(a.token_idx, a.spec) } },
        ["both", "long", "short"].map((d) => h("option", { value: d, selected: (f.direction || "both") === d }, d))); } },
    ],
  });
  const paintFollowers = () => fTable.update(known.filter((a) => accountKey(a.token_idx, a.spec) !== leaderSel.value));
  paintFollowers();
  leaderSel.addEventListener("change", paintFollowers);
  const pick = (cls, key) => fTable.tbody.querySelector(`.${cls}[data-key="${CSS.escape(key)}"]`);
  const collectFollowers = () => known.map((a) => {
    const key = accountKey(a.token_idx, a.spec);
    const on = pick("cp-on", key);
    if (!on || !on.checked) return null;
    return { token_idx: a.token_idx, lid: a.lid || "", spec: a.spec, account_id: a.id, enabled: true,
      mode: pick("cp-mode", key).value, multiplier: Number(pick("cp-mult", key).value) || 1,
      fixed: Number(pick("cp-fixed", key).value) || 1, max_contracts: Number(pick("cp-max", key).value) || 0,
      direction: pick("cp-dir", key).value };
  }).filter(Boolean);

  const body = () => {
    const lead = known.find((a) => accountKey(a.token_idx, a.spec) === leaderSel.value);
    return {
      name: nameInp.value.trim() || g.name, enabled: enabledSw.checked,
      leader: lead ? { token_idx: lead.token_idx, lid: lead.lid || "", spec: lead.spec, account_id: lead.id } : undefined,
      symbols: collectSymbols(), followers: collectFollowers(), feed: feedSel.value,
      feed_loss_flatten_s: Number(lossInp.value) || 30, copy_adds: addsSw.checked, copy_orders: ordersSw.checked,
    };
  };

  // --- Live status
  const liveBox = h("div");
  function paintLive(st) {
    clear(liveBox);
    if (!st) { liveBox.append(h("p", { class: "hint" }, g.enabled ? "Starting…" : "Group is off — enable it to start mirroring.")); return; }
    const head = h("div", { class: "inline-actions", style: "flex-wrap:wrap;margin-bottom:8px" },
      st.paused ? tag("paused", "warn") : !st.feed_ok ? tag("feed lost", "off") : st.feed === "websocket" ? tag(st.ws_ok ? "socket + poll · live" : "poll · live (socket down)", "on") : tag("poll · live", "on"),
      h("span", { class: "muted" }, `latency ${latencyText(st)}`),
      st.last_event_ts ? h("span", { class: "muted" }, `last leader change ${fmtTime(st.last_event_ts)}`) : null);
    const notes = [];
    if (st.pause_reason) notes.push(h("div", { class: "callout warn" }, st.pause_reason));
    if (st.error) notes.push(h("div", { class: "callout danger" }, st.error));
    if (st.feed === "websocket" && !st.ws_ok && st.ws_error) notes.push(h("div", { class: "hint" }, "Socket accelerator down (the 1-second poll carries the feed): ", st.ws_error));
    const rows = [];
    if (st.orders_error) notes.push(h("div", { class: "callout danger" }, "Orders: ", st.orders_error));
    for (const f of st.followers || []) {
      if (f.error) rows.push(h("div", { class: "callout danger" }, h("code", null, maskAccount(f.spec)), " ", f.error));
      for (const o of f.orders || []) {
        rows.push(h("div", { class: "cp-pos" }, h("code", null, maskAccount(f.spec)), h("span", null, o.symbol), tag("working order", "accent"), h("span", null, orderText(o)),
          h("span", { class: "muted" }, `twin of leader #${o.leader_order_id}`)));
      }
      for (const p of f.positions || []) {
        rows.push(h("div", { class: "cp-pos" }, h("code", null, maskAccount(f.spec)), h("span", null, p.symbol),
          h("span", { class: "muted" }, `leader ${signed(p.leader)}`), h("span", null, `target ${signed(p.target)}`),
          h("span", { class: p.actual === p.target ? "pos" : "neg" }, `actual ${signed(p.actual)}`),
          p.baseline ? tag("baseline · not copied", "warn") : null));
      }
    }
    const diag = st.diag || {};
    const diagLines = [
      `leader account id: ${diag.leader_account_id || "unknown"}`,
      st.feed === "websocket" ? `user id: ${diag.user_id || "—"} · frames: ${diag.frames || 0} · sync: ${diag.sync ? `${diag.sync.status} (${diag.sync.positions} position(s), accounts ${(diag.sync.accounts || []).join("/") || "—"})` : "no response yet"}` : null,
      st.feed === "websocket" ? `events: ${Object.entries(diag.props || {}).map(([k, v]) => `${k} ${v}`).join(", ") || "none"} · backstop catches: ${diag.backstop_catches || 0}` : null,
      diag.last_position_event ? `last position event: ${JSON.stringify(diag.last_position_event)}` : null,
      (diag.baseline || []).length ? `baseline (not copied): ${diag.baseline.join(", ")}` : null,
    ].filter(Boolean);
    const recent = (diag.recent || []).length ? h("pre", { class: "code cp-diag-pre" }, "recent socket messages (newest last):\n" + diag.recent.join("\n")) : null;
    const diagBox = h("details", { class: "cp-diag" }, h("summary", null, "Diagnostics"), h("pre", { class: "code cp-diag-pre" }, diagLines.join("\n")), recent);
    liveBox.append(...[head, ...notes, (st.leader_positions || []).length
      ? h("div", { class: "muted", style: "font-size:12px;margin-bottom:6px" }, "Leader: ", st.leader_positions.map((p) => `${p.symbol} ${signed(p.net)}${p.baseline ? " (baseline)" : ""}`).join(" · "))
      : h("div", { class: "muted", style: "font-size:12px;margin-bottom:6px" }, "Leader is flat."),
      (st.leader_orders || []).length ? h("div", { class: "muted", style: "font-size:12px;margin-bottom:6px" }, "Leader working orders: ", st.leader_orders.map((o) => `${o.symbol} ${orderText(o)}`).join(" · ")) : null,
      ...rows, diagBox].filter(Boolean));
  }
  paintLive(g.status);

  const act = (path, okMsg, opts = {}) => h("button", { type: "button", class: `btn btn-sm ${opts.cls || ""}`, onClick: async () => {
    if (opts.confirm && !(await confirmDialog(opts.confirm))) return;
    try { const r = await api.post(`/api/copy/groups/${g.id}/${path}`); toast(okMsg, "success"); paintLive(r); reload(); }
    catch (e) { toast(e.message, "error"); }
  } }, opts.icon ? icon(opts.icon) : null, opts.label);
  const actionRow = isNew ? null : h("div", { class: "inline-actions", style: "flex-wrap:wrap;margin-top:8px" },
    act("resume", "Group resumed", { label: "Resume", icon: "play" }),
    act("sync", "Synced to the leader", { label: "Sync now", icon: "refresh", confirm: { title: "Copy the leader's current positions now?", body: "Every follower gets a market order to match the leader's open positions right away (including positions that existed before the group started).", confirmText: "Sync now" } }),
    act("flatten", "Followers flattened", { label: "Flatten followers", icon: "alert", cls: "btn-danger", confirm: { title: "Flatten all followers?", body: "Every mirrored position on every follower is closed at market and the group pauses until you resume it.", confirmText: "Flatten", danger: true } }));

  // --- Save / delete
  const saveBtn = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
    const b = body();
    if (!b.leader) return toast("Choose a leader account", "error");
    if (!b.followers.length) return toast("Switch on at least one follower", "error");
    saveBtn.disabled = true;
    try {
      const saved = isNew ? await api.post("/api/copy/groups", b) : await api.put(`/api/copy/groups/${g.id}`, b);
      g = { ...saved };
      toast(isNew ? "Copy group created" : "Copy group saved", "success");
      closeDrawer();
      reload();
    } catch (e) { toast(e.message, "error"); }
    finally { saveBtn.disabled = false; }
  } }, icon("check"), isNew ? "Create group" : "Save changes");
  const delBtn = isNew ? null : h("button", { type: "button", class: "btn btn-ghost btn-danger", onClick: async () => {
    if (!(await confirmDialog({ title: `Delete "${g.name}"?`, body: "Followers keep whatever positions they hold — nothing is closed.", confirmText: "Delete", danger: true }))) return;
    try { await api.delete(`/api/copy/groups/${g.id}`); toast("Copy group deleted", "success"); closeDrawer(); reload(); }
    catch (e) { toast(e.message, "error"); }
  } }, icon("trash"), "Delete");

  let timer = null;
  if (!isNew) {
    timer = setInterval(async () => {
      try { const st = await api.get("/api/copy/status"); paintLive(st[g.id] || null); } catch { /* keep the last picture */ }
    }, 3000);
  }

  openDrawer({
    title: isNew ? "New copy group" : g.name,
    width: "760px",
    onClose: () => { if (timer) clearInterval(timer); if (onClose) onClose(); },
    body: [
      h("div", { class: "grid grid-2" },
        h("div", { class: "field" }, h("label", null, "Name"), nameInp),
        h("div", { class: "field" }, h("label", null, "Leader account"), leaderSel, h("div", { class: "field-hint" }, "Every position change on this account is mirrored onto the followers below."))),
      h("label", { class: "switch-row" }, h("span", null, "Group active", h("small", null, "Off = nothing is mirrored. Your Trading switch (Settings → General) applies as well.")), enabledSw),
      h("div", { class: "grid grid-2" },
        h("div", { class: "field" }, h("label", null, "Feed"), feedSel, h("div", { class: "field-hint" }, "Logins that execute through an agent are always polled through that agent. On the WebSocket feed a REST check every 5 s catches anything the socket missed.")),
        h("div", { class: "field" }, h("label", null, "Symbols"),
          h("label", { class: "switch-row", style: "padding-top:4px" }, h("span", null, "Every contract the leader trades"), allSw),
          symBox,
          h("div", { class: "field-hint" }, "Roots from Settings → Symbol Mapping; a dated contract such as MNQU6 counts as MNQ."))),
      h("div", { class: "grid grid-2" },
        h("div", { class: "field" }, h("label", null, "Flatten followers after feed loss (seconds)"), lossInp, h("div", { class: "field-hint" }, "No leader feed for this long → every follower's mirrored position is closed at market and the group pauses.")),
        h("label", { class: "switch-row" }, h("span", null, "Fixed mode follows adds / reductions", h("small", null, "On: 2 fixed contracts become 4 when the leader doubles up. Off: always the fixed size.")), addsSw)),
      h("label", { class: "switch-row" }, h("span", null, "Mirror working orders (limits, stops, brackets)", h("small", null, "Every working limit / stop order of the leader gets a twin on each follower, sized by the same rule, following the leader's modifications and cancelled when the leader's order is gone. A stop / target pair becomes an OCO pair on the follower. When a leader order fills, the follower's twin is cancelled first and the follower's real broker position decides the market order — a twin that already filled is never doubled.")), ordersSw),
      h("h3", null, "Followers"),
      h("p", { class: "hint" }, "Multiplier: leader size × factor (rounded, never below 1 while the leader holds). Fixed: this many contracts for the leader's entry. Max caps the size; Direction copies only longs or only shorts."),
      fTable.el,
      h("div", { class: "callout", style: "margin-top:10px" }, "Positions the leader already holds when the group starts are not copied (baseline). Mirroring of such a contract begins once the leader is flat again — or right away with Sync now."),
      h("h3", null, "Live"),
      liveBox, actionRow,
    ],
    foot: [saveBtn, h("button", { type: "button", class: "btn btn-ghost", onClick: () => closeDrawer() }, "Close"), h("span", { style: "flex:1" }), delBtn],
  });
}

export default {
  title: "Copy Trading",
  render(root, { navigate, params }) {
    let groups = [];
    const table = dataTable({
      empty: "No copy groups yet — pick a leader account and the accounts that should follow it.",
      onRow: (g) => navigate(`/copy/${g.id}`),
      columns: [
        { label: "On", render: (g) => h("input", { type: "checkbox", class: "switch", checked: !!g.enabled, title: "Enable / disable", onChange: async (e) => {
          try { await api.post(`/api/copy/groups/${g.id}/${e.target.checked ? "enable" : "disable"}`); toast(e.target.checked ? "Copy group enabled" : "Copy group disabled", "success"); load(); }
          catch (err) { e.target.checked = !e.target.checked; toast(err.message, "error"); }
        } }) },
        { label: "Name", render: (g) => h("span", { class: "wh-name" }, g.name) },
        { label: "Leader", render: (g) => h("code", null, g.leader && g.leader.spec ? maskAccount(g.leader.spec) : "—") },
        { label: "Symbols", render: (g) => (g.symbols || []).length ? g.symbols.join(", ") : h("span", { class: "muted" }, "all") },
        { label: "Followers", className: "num", render: (g) => String((g.followers || []).filter((f) => f.enabled).length) },
        { label: "Feed", render: feedTag },
        { label: "Latency", className: "num", render: (g) => latencyText(g.status) },
        { label: "Note", render: (g) => { const s = g.status || {}; const t = s.pause_reason || s.error || ""; return h("span", { class: "muted cp-note", title: t }, t); } },
        { label: "", render: (g) => h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: () => navigate(`/copy/${g.id}`) }, "Edit", icon("chevron")) },
      ],
    });
    const events = dataTable({
      compact: true,
      empty: "No copy events yet.",
      columns: [
        { label: "Time", render: (e) => fmtDateTime(e.ts) },
        { label: "Group", render: (e) => (groups.find((g) => g.id === e.group_id) || {}).name || e.group_id },
        { label: "Event", render: (e) => tag(e.kind.replace("_", " "), KIND_TONE[e.kind] ?? "") },
        { label: "Follower", render: (e) => e.follower ? h("code", null, maskAccount(e.follower)) : "—" },
        { label: "Symbol", render: (e) => e.symbol || "—" },
        { label: "Detail", render: (e) => h("span", { class: "cp-detail" }, e.detail) },
        { label: "Latency", className: "num", render: (e) => e.latency_ms != null ? `${e.latency_ms} ms` : "—" },
      ],
    });

    async function load() {
      try {
        groups = await api.get("/api/copy/groups");
        table.update(groups);
      } catch (e) { toast(e.message, "error"); }
    }
    async function loadEvents() {
      try { events.update(await api.get("/api/copy/events?limit=80")); } catch { /* transient */ }
    }
    const addBtn = h("button", { class: "btn btn-primary", onClick: async () => {
      if (!(store.get("tradeAccounts") || []).length) await actions.loadTradeAccounts();
      if (!store.get("settings")) await actions.loadSettings().catch(() => {});
      groupDrawer({ name: `Copy group ${groups.length + 1}`, enabled: false, followers: [], symbols: [], feed: "auto", feed_loss_flatten_s: 30, copy_adds: true, copy_orders: true }, { reload: () => { load(); loadEvents(); }, onClose: null });
    } }, icon("plus"), "Add copy group");

    root.append(
      pageHead("Copy Trading", "Mirror one leader trade account onto any number of follower accounts, live: entries, adds, reductions, closes and reversals. Followers are sized by multiplier or a fixed number of contracts; a lost leader feed flattens them after a grace period.", [
        h("button", { class: "btn", onClick: () => { load(); loadEvents(); } }, icon("refresh"), "Refresh"), addBtn,
      ]),
      card({ title: "Copy groups" }, table.el),
      card({ title: "Event log", hint: "Last 80 copy events (kept for 7 days): mirrored orders with their latency, rejects, drift corrections, feed changes." }, events.el),
    );
    let leaving = false, openId = null;
    const openFor = (id) => {
      const g = groups.find((x) => x.id === id);
      if (!g) { if (id) navigate("/copy", { replace: true }); return; }
      if (openId === id) return;
      openId = id;
      groupDrawer(g, { reload: () => { load(); loadEvents(); }, onClose: () => { openId = null; if (!leaving) navigate("/copy", { replace: true }); } });
    };
    const unsub = store.subscribe("route", (r) => { if (r && r.path.startsWith("/copy") && r.params.id && groups.length) openFor(r.params.id); });
    const boot = (async () => {
      if (!(store.get("tradeAccounts") || []).length) await actions.loadTradeAccounts();
      if (!store.get("settings")) await actions.loadSettings().catch(() => {});
      await load();
      await loadEvents();
      if (params && params.id && !leaving) openFor(params.id);
    })();
    const timer = setInterval(() => { load(); loadEvents(); }, 5000);
    return () => { leaving = true; clearInterval(timer); unsub(); openId = null; closeDrawer(); boot.catch(() => {}); };
  },
};
