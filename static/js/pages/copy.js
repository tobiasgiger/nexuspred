/* Copy Trading: a leader trade account mirrored onto follower accounts in
   real time. A group = leader + symbol filter + followers with sizing. The
   table shows the live feed / latency / pause state per group; a drawer edits
   the group and shows leader vs follower positions and the group's actions.
   Deep link: #/copy/<id>. */
import { h, card, tag, toast, confirmDialog, pageHead, clear, fmtDateTime, fmtTime } from "../ui.js";
import { maskAccount } from "../privacy.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { store, can } from "../store.js";
import { actions } from "../actions.js";
import { dataTable } from "../components/table.js";
import { openDrawer, closeDrawer } from "../components/drawer.js";
import { openCopySubscriptionDrawer } from "./marketplace.js";
import { publisherControls, subscriberStatusTag, subscriberActions } from "../components/publisher.js";
import { t } from "../i18n.js";

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
const orderText = (o) => `${o.action} ${o.qty} ${o.type}${o.price != null ? ` @ ${o.price}` : ""}${o.stop != null ? ` stop ${o.stop}` : ""}${o.oco ? t(" · OCO") : ""}`;

function feedTag(g) {
  const s = g.status;
  if (!g.enabled) return tag("off", "off");
  if (!s) return tag("starting…", "");
  if (s.paused) return tag("paused", "warn");
  if (!s.feed_ok) return tag("feed lost", "off");
  if (s.throttled) return tag("live · throttled", "warn");
  if (s.feed === "websocket") return tag(s.ws_ok ? t("socket + poll · live") : t("poll · live (socket down)"), "on");
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
    h("option", { value: "", selected: isNew }, t("— choose the leader account —")),
    known.map((a) => h("option", { value: accountKey(a.token_idx, a.spec), selected: !isNew && a.token_idx === g.leader.token_idx && a.spec === g.leader.spec },
      `${maskAccount(a.spec)} · ${a.token_name} · ${(a.environment || "").toUpperCase()}${a.agent_id ? t(" · via agent") : ""}`)));
  // --- Symbols: chips from the symbol mapping + free text for anything else
  const roots = knownRoots(store.get("settings"));
  const chosen = new Set((g.symbols || []).map(baseRoot));
  const allSw = h("input", { type: "checkbox", class: "switch", checked: !chosen.size });
  const chips = h("div", { class: "check-list cp-syms" }, roots.map((r) => h("label", null, h("input", { type: "checkbox", class: "cp-sym", value: r, checked: chosen.has(r) }), r)));
  const extraInp = h("input", { value: [...chosen].filter((r) => !roots.includes(r)).join(", "), placeholder: t("other roots, e.g. CL, RTY") });
  const symBox = h("div", { class: chosen.size ? "" : "hidden" }, chips, h("div", { class: "field", style: "margin-top:6px" }, extraInp));
  allSw.addEventListener("change", () => symBox.classList.toggle("hidden", allSw.checked));
  const collectSymbols = () => allSw.checked ? [] : [...new Set([...[...chips.querySelectorAll(".cp-sym:checked")].map((c) => c.value),
    ...extraInp.value.split(/[,;\s]+/).map(baseRoot).filter(Boolean)])];
  const feedSel = h("select", null,
    h("option", { value: "auto", selected: (g.feed || "auto") === "auto" }, t("Auto (WebSocket, poll for agent logins)")),
    h("option", { value: "websocket", selected: g.feed === "websocket" }, t("WebSocket (user sync, ~100 ms)")),
    h("option", { value: "poll", selected: g.feed === "poll" }, t("Poll every second")));
  const lossInp = h("input", { type: "number", min: 5, max: 600, value: g.feed_loss_flatten_s ?? 30, style: "width:120px" });
  const addsSw = h("input", { type: "checkbox", class: "switch", checked: g.copy_adds !== false });
  const ordersSw = h("input", { type: "checkbox", class: "switch", checked: !!g.copy_orders });
  const lossSel = h("select", null, h("option", { value: "flatten", selected: (g.on_feed_loss || "flatten") === "flatten" }, t("Flatten followers, then pause")),
    h("option", { value: "pause", selected: g.on_feed_loss === "pause" }, t("Pause only (followers keep their positions)")));

  // --- Followers
  const selected = new Map((g.followers || []).map((f) => [accountKey(f.token_idx, f.spec), f]));
  const fTable = dataTable({
    compact: true,
    empty: t("No trade accounts discovered yet — add a login under Settings → Broker Accounts and Connect & Verify."),
    columns: [
      { label: t("Follow"), render: (a) => h("input", { type: "checkbox", class: "switch cp-on", checked: !!(selected.get(accountKey(a.token_idx, a.spec)) || {}).enabled && selected.has(accountKey(a.token_idx, a.spec)), dataset: { key: accountKey(a.token_idx, a.spec) } }) },
      { label: t("Account"), render: (a) => h("span", null, h("code", null, maskAccount(a.spec)), h("small", { class: "muted", style: "display:block" }, `${a.token_name} · ${(a.environment || "").toUpperCase()}`)) },
      { label: t("Mode"), render: (a) => { const f = selected.get(accountKey(a.token_idx, a.spec)) || {}; return h("select", { class: "cp-mode input-sm", dataset: { key: accountKey(a.token_idx, a.spec) } },
        h("option", { value: "multiplier", selected: (f.mode || "multiplier") === "multiplier" }, t("Multiplier")), h("option", { value: "fixed", selected: f.mode === "fixed" }, t("Fixed"))); } },
      { label: "×", render: (a) => h("input", { type: "number", class: "cp-mult input-sm", min: 0.01, step: 0.01, style: "width:70px", value: (selected.get(accountKey(a.token_idx, a.spec)) || {}).multiplier ?? 1, dataset: { key: accountKey(a.token_idx, a.spec) } }) },
      { label: t("Fixed"), render: (a) => h("input", { type: "number", class: "cp-fixed input-sm", min: 1, step: 1, style: "width:64px", value: (selected.get(accountKey(a.token_idx, a.spec)) || {}).fixed ?? 1, dataset: { key: accountKey(a.token_idx, a.spec) } }) },
      { label: t("Max"), render: (a) => h("input", { type: "number", class: "cp-max input-sm", min: 0, step: 1, style: "width:64px", title: t("0 = no cap"), value: (selected.get(accountKey(a.token_idx, a.spec)) || {}).max_contracts ?? 0, dataset: { key: accountKey(a.token_idx, a.spec) } }) },
      { label: t("Direction"), render: (a) => { const f = selected.get(accountKey(a.token_idx, a.spec)) || {}; return h("select", { class: "cp-dir input-sm", dataset: { key: accountKey(a.token_idx, a.spec) } },
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
      feed_loss_flatten_s: Number(lossInp.value) || 30, on_feed_loss: lossSel.value, copy_adds: addsSw.checked, copy_orders: ordersSw.checked,
    };
  };

  // --- Live status
  const liveBox = h("div");
  function paintLive(st) {
    clear(liveBox);
    if (!st) { liveBox.append(h("p", { class: "hint" }, g.enabled ? t("Starting…") : t("Group is off — enable it to start mirroring."))); return; }
    const head = h("div", { class: "inline-actions", style: "flex-wrap:wrap;margin-bottom:8px" },
      st.paused ? tag("paused", "warn") : !st.feed_ok ? tag("feed lost", "off") : st.feed === "websocket" ? tag(st.ws_ok ? t("socket + poll · live") : t("poll · live (socket down)"), "on") : tag("poll · live", "on"),
      h("span", { class: "muted" }, `latency ${latencyText(st)}`),
      st.last_event_ts ? h("span", { class: "muted" }, `last leader change ${fmtTime(st.last_event_ts)}`) : null);
    const notes = [];
    if (st.pause_reason) notes.push(h("div", { class: "callout warn" }, st.pause_reason));
    if (st.error) notes.push(h("div", { class: "callout danger" }, st.error));
    if (st.feed === "websocket" && !st.ws_ok && st.ws_error) notes.push(h("div", { class: "hint" }, t("Socket accelerator down (the 1-second poll carries the feed): "), st.ws_error));
    const rows = [];
    if (st.orders_error) notes.push(h("div", { class: "callout danger" }, t("Orders: "), st.orders_error));
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
    const diagBox = h("details", { class: "cp-diag" }, h("summary", null, t("Diagnostics")), h("pre", { class: "code cp-diag-pre" }, diagLines.join("\n")), recent);
    liveBox.append(...[head, ...notes, (st.leader_positions || []).length
      ? h("div", { class: "muted", style: "font-size:12px;margin-bottom:6px" }, t("Leader: "), st.leader_positions.map((p) => `${p.symbol} ${signed(p.net)}${p.baseline ? t(" (baseline)") : ""}`).join(" · "))
      : h("div", { class: "muted", style: "font-size:12px;margin-bottom:6px" }, t("Leader is flat.")),
      (st.leader_orders || []).length ? h("div", { class: "muted", style: "font-size:12px;margin-bottom:6px" }, t("Leader working orders: "), st.leader_orders.map((o) => `${o.symbol} ${orderText(o)}`).join(" · ")) : null,
      ...rows, diagBox].filter(Boolean));
  }
  paintLive(g.status);

  const act = (path, okMsg, opts = {}) => {
    const btn = h("button", { type: "button", class: `btn btn-sm ${opts.cls || ""}`, onClick: async () => {
      if (btn.disabled) return;
      if (opts.confirm && !(await confirmDialog(opts.confirm))) return;
      btn.disabled = true;                                   // one request per click, never two
      try { const r = await api.post(`/api/copy/groups/${g.id}/${path}`); toast(okMsg, "success"); paintLive(r); reload(); }
      catch (e) { toast(e.message, "error"); }
      finally { btn.disabled = false; }
    } }, opts.icon ? icon(opts.icon) : null, opts.label);
    return btn;
  };
  const actionRow = isNew ? null : h("div", { class: "inline-actions", style: "flex-wrap:wrap;margin-top:8px" },
    act("resume", "Group resumed", { label: t("Resume"), icon: "play" }),
    act("sync", "Synced to the leader", { label: t("Sync now"), icon: "refresh", confirm: { title: t("Copy the leader's current positions now?"), body: t("Every follower gets a market order to match the leader's open positions right away (including positions that existed before the group started)."), confirmText: t("Sync now") } }),
    act("flatten", "Followers flattened", { label: t("Flatten followers"), icon: "alert", cls: "btn-danger", confirm: { title: t("Flatten all followers?"), body: t("Every mirrored position on every follower is closed at market and the group pauses until you resume it."), confirmText: t("Flatten"), danger: true } }));

  // --- Save / delete
  const saveBtn = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
    const b = body();
    if (!b.leader) return toast(t("Choose a leader account"), "error");
    if (!b.followers.length) return toast(t("Switch on at least one follower"), "error");
    saveBtn.disabled = true;
    try {
      const saved = isNew ? await api.post("/api/copy/groups", b) : await api.put(`/api/copy/groups/${g.id}`, b);
      g = { ...saved };
      toast(isNew ? t("Copy group created") : t("Copy group saved"), "success");
      closeDrawer();
      reload();
    } catch (e) { toast(e.message, "error"); }
    finally { saveBtn.disabled = false; }
  } }, icon("check"), isNew ? t("Create group") : t("Save changes"));
  const delBtn = isNew ? null : h("button", { type: "button", class: "btn btn-ghost btn-danger", onClick: async () => {
    if (!(await confirmDialog({ title: t("Delete \"{name}\"?", { name: g.name }), body: t("Followers keep whatever positions they hold — nothing is closed."), confirmText: t("Delete"), danger: true }))) return;
    try { await api.del(`/api/copy/groups/${g.id}`); toast(t("Copy group deleted"), "success"); closeDrawer(); reload(); }
    catch (e) { toast(e.message, "error"); }
  } }, icon("trash"), t("Delete"));

  // --- Marketplace (admins, saved groups): publish the group as a product
  let sharingPane = null;
  const me = store.get("me");
  if (!isNew && can(me, "admin")) {
    const sh = { enabled: false, title: "", description: "", visibility: "all", allowed_user_ids: [], ...(g.sharing || {}) };
    const pubSw = h("input", { type: "checkbox", class: "switch", checked: !!sh.enabled });
    const titleInp = h("input", { value: sh.title || "", placeholder: g.name, maxlength: 80 });
    const descTa = h("textarea", { rows: 3, maxlength: 1000, placeholder: t("What the leader trades, typical size, session…"), style: "font-family:inherit" }, sh.description || "");
    const visSel = h("select", null, h("option", { value: "all", selected: sh.visibility !== "selected" }, t("Every registered user")), h("option", { value: "selected", selected: sh.visibility === "selected" }, t("Only selected users")));
    const userList = h("div", { class: "check-list" }, h("span", { class: "muted" }, t("Loading users…")));
    const userBox = h("div", { class: `field ${sh.visibility === "selected" ? "" : "hidden"}` }, h("label", null, t("Allowed users")), userList);
    visSel.addEventListener("change", () => userBox.classList.toggle("hidden", visSel.value !== "selected"));
    api.get("/api/users").then((r) => {
      const users = (r.users || r).filter((u) => u.id !== me.id);
      clear(userList);
      if (!users.length) userList.append(h("span", { class: "muted" }, t("No other users yet — invite them under Settings → Users.")));
      userList.append(users.map((u) => h("label", null, h("input", { type: "checkbox", class: "allow-user", value: String(u.id), checked: (sh.allowed_user_ids || []).includes(u.id) }), u.email)));
    }).catch(() => { clear(userList); userList.append(h("span", { class: "muted" }, t("Could not load users."))); });
    const pub = publisherControls(sh);
    const subsTable = dataTable({ empty: t("No followers from the marketplace yet."), compact: true, columns: [
      { label: t("Follower"), render: (s) => s.email },
      { label: t("Status"), render: (s) => subscriberStatusTag(s) },
      { label: t("Accounts"), className: "num", render: (s) => String(s.accounts) },
      { label: t("Since"), render: (s) => fmtDateTime(s.created_at) },
      { label: "", render: (s) => h("div", { class: "inline-actions" }, subscriberActions(s, `/api/copy/groups/${g.id}/subscribers`, loadSubs), h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: async () => {
        if (!(await confirmDialog({ title: t("Remove {email}?", { email: s.email }), body: t("Their accounts leave the mirror immediately (positions are not closed). They can follow again unless you restrict visibility."), confirmText: t("Remove"), danger: true }))) return;
        try { await api.del(`/api/copy/groups/${g.id}/subscribers/${s.id}`); toast(t("Follower removed"), "success"); loadSubs(); }
        catch (e) { toast(e.message, "error"); }
      } }, icon("trash"), t("Remove"))) },
    ] });
    const loadSubs = () => api.get(`/api/copy/groups/${g.id}/subscribers`).then((list) => subsTable.update(list)).catch(() => subsTable.update([]));
    loadSubs();
    const shareBtn = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
      shareBtn.disabled = true;
      try {
        const updated = await api.put(`/api/copy/groups/${g.id}/sharing`, {
          enabled: pubSw.checked, title: titleInp.value.trim(), description: descTa.value.trim(), visibility: visSel.value,
          allowed_user_ids: [...userList.querySelectorAll(".allow-user:checked")].map((c) => Number(c.value)), ...pub.collect(),
        });
        g = { ...g, sharing: updated.sharing };
        toast(pubSw.checked ? t("Published on the marketplace") : t("Sharing saved"), "success");
        reload();
      } catch (e) { toast(e.message, "error"); } finally { shareBtn.disabled = false; }
    } }, icon("share"), t("Save sharing"));
    sharingPane = h("div", null,
      h("h3", null, t("Marketplace")),
      h("label", { class: "switch-row" }, h("span", null, t("Publish this leader on the marketplace"), h("small", null, t("Other users can follow with their own accounts — mirrored by this group, on their logins, under their trading switch and risk locks. They never see your accounts; you never see theirs (followers appear as “subscriber #n”). Unpublishing removes their accounts from the mirror."))), pubSw),
      h("div", { class: "grid grid-2", style: "margin-top:14px" },
        h("div", { class: "field" }, h("label", null, t("Title shown to followers")), titleInp),
        h("div", { class: "field" }, h("label", null, t("Visibility")), visSel)),
      h("div", { class: "field" }, h("label", null, t("Description")), descTa),
      userBox,
      pub.el,
      h("div", { class: "form-actions" }, shareBtn),
      h("h3", null, t("Followers from the marketplace")), subsTable.el);
  }

  let timer = null;
  if (!isNew) {
    timer = setInterval(async () => {
      try { const st = await api.get("/api/copy/status"); paintLive(st[g.id] || null); } catch { /* keep the last picture */ }
    }, 3000);
  }

  openDrawer({
    title: isNew ? t("New copy group") : g.name,
    width: "760px",
    onClose: () => { if (timer) clearInterval(timer); if (onClose) onClose(); },
    body: [
      h("div", { class: "grid grid-2" },
        h("div", { class: "field" }, h("label", null, t("Name")), nameInp),
        h("div", { class: "field" }, h("label", null, t("Leader account")), leaderSel, h("div", { class: "field-hint" }, t("Every position change on this account is mirrored onto the followers below.")))),
      h("label", { class: "switch-row" }, h("span", null, t("Group active"), h("small", null, t("Off = nothing is mirrored. Your Trading switch (Settings → General) applies as well."))), enabledSw),
      h("div", { class: "grid grid-2" },
        h("div", { class: "field" }, h("label", null, t("Feed")), feedSel, h("div", { class: "field-hint" }, t("Logins that execute through an agent are always polled through that agent. On the WebSocket feed a REST check every 5 s catches anything the socket missed."))),
        h("div", { class: "field" }, h("label", null, t("Symbols")),
          h("label", { class: "switch-row", style: "padding-top:4px" }, h("span", null, t("Every contract the leader trades")), allSw),
          symBox,
          h("div", { class: "field-hint" }, t("Roots from Settings → Symbol Mapping; a dated contract such as MNQU6 counts as MNQ.")))),
      h("div", { class: "grid grid-2" },
        h("div", { class: "field" }, h("label", null, t("After feed loss of (seconds)")), h("div", { style: "display:flex;gap:8px;flex-wrap:wrap" }, lossInp, lossSel), h("div", { class: "field-hint" }, t("No leader feed (broker unreachable, token or login dead, agent offline) for this long → the chosen action. Flatten closes every mirrored follower position at market."))),
        h("label", { class: "switch-row" }, h("span", null, t("Fixed mode follows adds / reductions"), h("small", null, t("On: 2 fixed contracts become 4 when the leader doubles up. Off: always the fixed size."))), addsSw)),
      h("label", { class: "switch-row" }, h("span", null, t("Mirror working orders (limits, stops, brackets)"), h("small", null, t("Every working limit / stop order of the leader gets a twin on each follower, sized by the same rule, following the leader's modifications and cancelled when the leader's order is gone. A stop / target pair becomes an OCO pair on the follower. When a leader order fills, the follower's twin is cancelled first and the follower's real broker position decides the market order — a twin that already filled is never doubled."))), ordersSw),
      h("h3", null, t("Followers")),
      h("p", { class: "hint" }, "Multiplier: leader size × factor (rounded, never below 1 while the leader holds). Fixed: this many contracts for the leader's entry. Max caps the size; Direction copies only longs or only shorts. A follower account is exclusive: the mirror treats its whole position in a contract as its own, so do not trade a follower by hand or through another route, and an account can follow one leader only."),
      fTable.el,
      h("div", { class: "callout", style: "margin-top:10px" }, t("Positions the leader already holds when the group starts are not copied (baseline). Mirroring of such a contract begins once the leader is flat again — or right away with Sync now.")),
      h("h3", null, t("Live")),
      liveBox, actionRow,
      sharingPane,
    ],
    foot: [saveBtn, h("button", { type: "button", class: "btn btn-ghost", onClick: () => closeDrawer() }, t("Close")), h("span", { style: "flex:1" }), delBtn],
  });
}

export default {
  title: t("Copy Trading"),
  render(root, { navigate, params }) {
    let groups = [];
    const table = dataTable({
      empty: t("No copy groups yet — pick a leader account and the accounts that should follow it."),
      onRow: (g) => navigate(`/copy/${g.id}`),
      columns: [
        { label: t("On"), render: (g) => h("input", { type: "checkbox", class: "switch", checked: !!g.enabled, title: t("Enable / disable"), onChange: async (e) => {
          try { await api.post(`/api/copy/groups/${g.id}/${e.target.checked ? "enable" : "disable"}`); toast(e.target.checked ? t("Copy group enabled") : t("Copy group disabled"), "success"); load(); }
          catch (err) { e.target.checked = !e.target.checked; toast(err.message, "error"); }
        } }) },
        { label: t("Name"), render: (g) => h("span", { class: "wh-name" }, g.name) },
        { label: t("Leader"), render: (g) => h("code", null, g.leader && g.leader.spec ? maskAccount(g.leader.spec) : "—") },
        { label: t("Symbols"), render: (g) => (g.symbols || []).length ? g.symbols.join(", ") : h("span", { class: "muted" }, "all") },
        { label: t("Followers"), className: "num", render: (g) => [String((g.followers || []).filter((f) => f.enabled).length), g.subscriber_count ? h("small", { class: "muted" }, ` +${g.subscriber_count} mkt`) : null] },
        { label: t("Shared"), render: (g) => (g.sharing && g.sharing.enabled) ? tag(`published · ${g.subscriber_count || 0}`, "accent") : h("span", { class: "muted" }, "—") },
        { label: t("Feed"), render: feedTag },
        { label: t("Latency"), className: "num", render: (g) => latencyText(g.status) },
        { label: t("Note"), render: (g) => { const s = g.status || {}; const note = s.pause_reason || s.error || ""; return h("span", { class: "muted cp-note", title: note }, note); } },
        { label: "", render: (g) => h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: () => navigate(`/copy/${g.id}`) }, t("Edit"), icon("chevron")) },
      ],
    });
    const events = dataTable({
      compact: true,
      empty: t("No copy events yet."),
      columns: [
        { label: t("Time"), render: (e) => fmtDateTime(e.ts) },
        { label: t("Group"), render: (e) => (groups.find((g) => g.id === e.group_id) || {}).name || e.group_id },
        { label: t("Event"), render: (e) => tag(e.kind.replace("_", " "), KIND_TONE[e.kind] ?? "") },
        { label: t("Follower"), render: (e) => e.follower ? h("code", null, maskAccount(e.follower)) : "—" },
        { label: t("Symbol"), render: (e) => e.symbol || "—" },
        { label: t("Detail"), render: (e) => h("span", { class: "cp-detail" }, e.detail) },
        { label: t("Latency"), className: "num", render: (e) => e.latency_ms != null ? `${e.latency_ms} ms` : "—" },
      ],
    });

    // --- Following: leaders this workspace follows through the marketplace
    const following = dataTable({
      compact: true,
      empty: t("You follow no leader from the marketplace. Marketplace → Follow."),
      columns: [
        { label: t("On"), render: (f) => h("input", { type: "checkbox", class: "switch", checked: !!f.enabled, title: t("Enable / disable"), onChange: async (e) => {
          try { await api.put(`/api/subscriptions/${f.sub_id}`, { enabled: e.target.checked }); toast(e.target.checked ? t("Following enabled") : t("Following disabled"), "success"); loadFollowing(); }
          catch (err) { e.target.checked = !e.target.checked; toast(err.message, "error"); }
        } }) },
        { label: t("Leader"), render: (f) => [h("span", { class: "wh-name" }, f.title), h("small", { class: "muted", style: "display:block" }, f.publisher_email)] },
        { label: t("Status"), render: (f) => !f.published ? tag("unpublished", "warn") : !f.enabled ? tag("off", "off") : f.paused ? tag("paused", "warn") : f.running && f.feed_ok ? tag("live", "on") : f.running ? tag("feed lost", "off") : tag("group off", "warn") },
        { label: t("My accounts"), render: (f) => h("div", null, (f.followers || []).length ? f.followers.map((a) => h("div", { class: "cp-pos" }, h("code", null, maskAccount(a.spec)),
          a.error ? h("span", { class: "neg" }, a.error) : null,
          ...(a.positions || []).map((p) => h("span", null, `${p.symbol} target ${signed(p.target)} · `, h("span", { class: p.actual === p.target ? "pos" : "neg" }, `actual ${signed(p.actual)}`), p.baseline ? t(" (baseline)") : "")),
          !(a.positions || []).length && !a.error ? h("span", { class: "muted" }, "flat") : null)) : (f.accounts || []).map((a) => h("code", null, maskAccount(a.spec)))) },
        { label: t("Leader positions"), render: (f) => (f.leader_positions || []).length ? f.leader_positions.map((p) => h("div", null, `${p.symbol} ${signed(p.net)}`)) : h("span", { class: "muted" }, "flat") },
        { label: t("Latency"), className: "num", render: (f) => f.latency_ms != null ? `${f.latency_ms} ms` : "—" },
        { label: "", render: (f) => h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: () => openCopySubscriptionDrawer({ publisher_area_id: f.publisher_area_id, group_id: f.group_id, title: f.title, publisher_email: f.publisher_email, symbols: f.symbols,
          subscription: { id: f.sub_id, enabled: f.enabled, accounts: f.accounts } }, loadFollowing) }, t("Manage"), icon("chevron")) },
      ],
    });
    let lastFollowingJson = "";
    async function loadFollowing() {
      try {
        const fresh = await api.get("/api/copy/following");
        const json = JSON.stringify(fresh);
        if (json !== lastFollowingJson) { lastFollowingJson = json; following.update(fresh); }
      } catch { /* transient */ }
    }
    const followingCard = card({ title: t("Following (marketplace)"), hint: t("Leaders you follow with your own accounts. The mirror runs in the leader's workspace; your Trading switch, risk locks and logs apply to your accounts.") }, following.el);

    let lastGroupsJson = "", lastEventsJson = "", outage = false;
    async function load() {
      try {
        const fresh = await api.get("/api/copy/groups");
        if (outage) { outage = false; toast(t("Copy trading reachable again"), "success"); }
        const json = JSON.stringify(fresh);
        groups = fresh;
        if (json !== lastGroupsJson) { lastGroupsJson = json; table.update(groups); }   // no DOM churn on identical polls
      } catch (e) {
        if (!outage) { outage = true; toast(e.message, "error"); }                     // one toast per outage, not one per tick
      }
    }
    async function loadEvents() {
      try {
        const fresh = await api.get("/api/copy/events?limit=80");
        const json = JSON.stringify(fresh);
        if (json !== lastEventsJson) { lastEventsJson = json; events.update(fresh); }
      } catch { /* transient */ }
    }
    const addBtn = h("button", { class: "btn btn-primary", onClick: async () => {
      if (!(store.get("tradeAccounts") || []).length) await actions.loadTradeAccounts();
      if (!store.get("settings")) await actions.loadSettings().catch(() => {});
      groupDrawer({ name: t("Copy group {n}", { n: groups.length + 1 }), enabled: false, followers: [], symbols: [], feed: "auto", feed_loss_flatten_s: 30, copy_adds: true, copy_orders: true }, { reload: () => { load(); loadEvents(); }, onClose: null });
    } }, icon("plus"), t("Add copy group"));

    root.append(
      pageHead(t("Copy Trading"), t("Mirror one leader trade account onto any number of follower accounts, live: entries, adds, reductions, closes and reversals. Followers are sized by multiplier or a fixed number of contracts; a lost leader feed flattens them after a grace period."), [
        h("button", { class: "btn", onClick: () => { load(); loadEvents(); } }, icon("refresh"), t("Refresh")), addBtn,
      ]),
      card({ title: t("Copy groups") }, table.el),
      followingCard,
      card({ title: t("Event log"), hint: t("Last 80 copy events (kept for 7 days): mirrored orders with their latency, rejects, drift corrections, feed changes.") }, events.el),
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
      await loadFollowing();
      const want = (store.get("route") || {}).params?.id || (params && params.id);   // the deep link current now
      if (want && !leaving) openFor(want);
    })();
    const timer = setInterval(() => { load(); loadEvents(); loadFollowing(); }, 5000);
    return () => { leaving = true; clearInterval(timer); unsub(); openId = null; closeDrawer(); boot.catch(() => {}); };
  },
};
