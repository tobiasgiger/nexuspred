/* Settings → Users (admin): invites, accounts & feature grants, password resets, audit log. */
import { h, card, tag, toast, confirmDialog, copyText, pageHead, fmtDateTime } from "../ui.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { store } from "../store.js";
import { dataTable } from "../components/table.js";

const ACTION_LABEL = {
  invite_create: "Invite created", invite_revoke: "Invite revoked", user_delete: "User deleted",
  feature_set: "Feature changed", password_reset: "Password reset", password_change: "Password changed",
  flatten_all: "Flatten all", subscribe: "Subscribed", unsubscribe: "Unsubscribed",
  webhook_share: "Marketplace publish", subscriber_remove: "Subscriber removed",
  login_ok: "Signed in", login_failed: "Failed sign-in", login_blocked: "Rate limited",
  agent_pairing_code: "Agent pairing code", agent_bundle: "Agent download (preconfigured)", agent_paired: "Agent paired", agent_pair_failed: "Agent pairing failed", agent_revoke: "Agent revoked",
};

export default {
  title: "Users",
  gate: "admin",
  render(root) {
    const me = store.get("me") || {};
    const linkBox = (label) => {
      const inp = h("input", { readonly: true, class: "mono input-sm", onClick: (e) => e.target.select() });
      const el = h("div", { class: "callout ok hidden" }, h("div", { class: "field", style: "margin:0" }, h("label", null, label), inp,
        h("div", { class: "form-actions", style: "margin-top:8px" }, h("button", { type: "button", class: "btn btn-sm btn-primary", onClick: async () => toast((await copyText(inp.value)) ? "Link copied" : "Copy failed", "success") }, icon("copy"), "Copy link"))));
      return { el, show(url, text) { inp.value = url; el.querySelector("label").textContent = text || label; el.classList.remove("hidden"); } };
    };

    // ---- invites
    const inviteEmail = h("input", { type: "email", placeholder: "name@example.com", autocomplete: "off" });
    const inviteAdmin = h("input", { type: "checkbox", class: "switch" });
    const inviteSend = h("input", { type: "checkbox", class: "switch" });
    const inviteLink = linkBox("Invite link — share it with the new user");
    const inviteBtn = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
      try {
        const email = inviteEmail.value.trim();
        // `elevated`, not `is_admin`: some WAFs block bodies containing is_admin.
        const r = await api.post("/api/users/invite", { elevated: inviteAdmin.checked, email, send_email: inviteSend.checked });
        const url = r.url || `${window.location.origin}/register?code=${r.code || ""}`;
        inviteLink.show(url);
        copyText(url);
        if (inviteSend.checked && email) toast(r.emailed ? `Invite emailed to ${email}` : "Invite created — email not sent (SMTP not configured)", r.emailed ? "success" : "warn");
        else toast("Invite link created and copied", "success");
        loadInvites(); loadAudit();
      } catch (e) { toast(e.message, "error"); }
    } }, icon("plus"), "Create invite");

    const invites = dataTable({ empty: "No open invites", columns: [
      { label: "Invite link", render: (i) => h("code", { style: "font-size:11px" }, `${window.location.origin}/register?code=${i.code}`) },
      { label: "For", render: (i) => i.email || "anyone" },
      { label: "Admin", render: (i) => i.is_admin ? tag("admin", "accent") : "—" },
      { label: "Created", render: (i) => fmtDateTime(i.created_at) },
      { label: "", render: (i) => h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: async () => {
        try { await api.del(`/api/invites/${i.code}`); toast("Invite revoked"); loadInvites(); loadAudit(); } catch (e) { toast(e.message, "error"); }
      } }, "Revoke") },
    ] });

    // ---- users
    const resetLink = linkBox("Password-reset link");
    const users = dataTable({ empty: "No users", columns: [
      { label: "Email", render: (u) => [u.email, u.id === me.id ? [" ", tag("you")] : null] },
      { label: "Role", render: (u) => u.is_admin ? tag("admin", "accent") : "user" },
      { label: "Discord Signals", render: (u) => h("input", { type: "checkbox", class: "switch", checked: (u.features || {}).discord_signals === true, title: "Grant the Discord listener module", onChange: async (e) => {
        try { await api.post(`/api/users/${u.id}/features`, { feature: "discord_signals", enabled: e.target.checked }); toast(`Discord Signals ${e.target.checked ? "enabled" : "disabled"} for ${u.email}`, "success"); loadAudit(); }
        catch (err) { e.target.checked = !e.target.checked; toast(err.message, "error"); }
      } }) },
      { label: "Created", render: (u) => fmtDateTime(u.created_at) },
      { label: "Last sign-in", render: (u) => u.last_login_at ? h("span", { title: u.last_login_ip ? `from ${u.last_login_ip}` : "" }, fmtDateTime(u.last_login_at)) : "never" },
      { label: "", render: (u) => h("div", { class: "users-actions" },
        h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: async () => {
          try {
            const r = await api.post(`/api/users/${u.id}/reset`);
            if (r.emailed) {
              toast(`Reset link emailed to ${u.email}`, "success");
            } else {
              resetLink.show(r.url, `Password-reset link for ${u.email} — single use, expires in 24 h`);
              copyText(r.url);
              toast("Reset link created and copied", "success");
            }
            loadAudit();
          } catch (e) { toast(e.message, "error"); }
        } }, icon("key"), "Reset password"),
        h("button", { type: "button", class: "btn btn-ghost btn-sm", title: "Log this user out of every browser and phone (lost device, leaked cookie).", onClick: async () => {
          if (!(await confirmDialog({ title: `Sign ${u.email} out everywhere?`, body: "Every session of this user is ended immediately; they sign in again with their password.", confirmText: "Sign out everywhere" }))) return;
          try { await api.post(`/api/users/${u.id}/sessions/revoke`); toast("Sessions revoked", "success"); loadAudit(); } catch (e) { toast(e.message, "error"); }
        } }, icon("logout"), "Sign out everywhere"),
        u.id === me.id ? null : h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: async () => {
          if (!(await confirmDialog({ title: `Delete ${u.email}?`, body: "Their area and all its data (webhooks, tokens, logs) are removed. This cannot be undone.", confirmText: "Delete user", danger: true }))) return;
          try { await api.del(`/api/users/${u.id}`); toast("User deleted", "success"); loadUsers(); loadAudit(); } catch (e) { toast(e.message, "error"); }
        } }, icon("trash"), "Delete")) },
    ] });

    const audit = dataTable({ empty: "No admin actions yet", compact: true, columns: [
      { label: "When", render: (r) => fmtDateTime(r.created_at) },
      { label: "Admin", render: (r) => r.actor_email || "—" },
      { label: "Action", render: (r) => ACTION_LABEL[r.action] || r.action },
      { label: "Target", render: (r) => r.target || "—" },
      { label: "Detail", render: (r) => r.detail || "" },
    ] });

    async function loadUsers() { try { const r = await api.get("/api/users"); users.update(r.users || r); } catch (e) { /* ignore */ } }
    async function loadInvites() { try { const list = await api.get("/api/invites"); invites.update(list.filter((i) => !i.used_by)); } catch (e) { /* ignore */ } }
    const logins = dataTable({ empty: "No sign-ins recorded yet", compact: true, columns: [
      { label: "When", render: (r) => fmtDateTime(r.created_at) },
      { label: "Email", render: (r) => r.actor_email || "—" },
      { label: "Result", render: (r) => r.action === "login_ok" ? tag("ok", "ok") : r.action === "login_blocked" ? tag("rate limited", "warn") : tag("failed", "error") },
      { label: "IP", render: (r) => h("code", null, r.target || "—") },
      { label: "Detail", render: (r) => r.detail || "" },
    ] });
    async function loadAudit() {
      try { audit.update(await api.get("/api/audit")); } catch (e) { /* ignore */ }
      try { logins.update(await api.get("/api/audit?kind=logins")); } catch (e) { /* ignore */ }
    }

    root.append(
      pageHead("Users", "Registration is invite-only. Every user gets their own isolated area; admins can invite, grant features and reset passwords."),
      card({ title: "Create invite" },
        h("div", { class: "grid grid-2" },
          h("div", { class: "field" }, h("label", null, "Invitee email (optional)"), inviteEmail, h("div", { class: "field-hint" }, "Pre-fills the sign-up form; leave empty for an open invite.")),
          h("div", null,
            h("label", { class: "switch-row" }, h("span", null, "New invite is admin", h("small", null, "They can manage users too.")), inviteAdmin),
            h("label", { class: "switch-row" }, h("span", null, "Email the invite link", h("small", null, "Requires SMTP under Settings → Alerts.")), inviteSend))),
        h("div", { class: "form-actions" }, inviteBtn), inviteLink.el),
      card({ title: "Accounts", hint: "Toggle Discord Signals to grant a user the Discord listener module — its navigation, settings and live connection appear only for users you enable it for." }, users.el, resetLink.el),
      card({ title: "Open invites" }, invites.el),
      card({ title: "Admin activity", hint: "Recent admin actions: invites, removals, feature grants, password resets, emergency flattens." }, audit.el),
      card({ title: "Sign-ins", hint: "Every successful, failed and rate-limited sign-in with the client IP (last 100)." }, logins.el),
    );
    loadUsers(); loadInvites(); loadAudit();
    return () => {};
  },
};
