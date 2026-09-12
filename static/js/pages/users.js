/* Settings → Users (admin): invites, accounts & feature grants, password resets, audit log. */
import { h, card, tag, toast, confirmDialog, copyText, pageHead, fmtDateTime } from "../ui.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { store } from "../store.js";
import { dataTable } from "../components/table.js";
import { t } from "../i18n.js";

const ACTION_LABEL = {
  invite_create: t("Invite created"), invite_revoke: t("Invite revoked"), user_delete: t("User deleted"),
  feature_set: t("Feature changed"), password_reset: t("Password reset"), password_change: t("Password changed"),
  flatten_all: t("Flatten all"), subscribe: t("Subscribed"), unsubscribe: t("Unsubscribed"),
  webhook_share: t("Marketplace publish"), subscriber_remove: t("Subscriber removed"),
  login_ok: t("Signed in"), login_failed: t("Failed sign-in"), login_blocked: t("Rate limited"),
  agent_pairing_code: t("Agent pairing code"), agent_bundle: t("Agent download (preconfigured)"), agent_paired: t("Agent paired"), agent_pair_failed: t("Agent pairing failed"), agent_revoke: t("Agent revoked"),
};

export default {
  title: t("Users"),
  gate: "admin",
  render(root) {
    const me = store.get("me") || {};
    const linkBox = (label) => {
      const inp = h("input", { readonly: true, class: "mono input-sm", onClick: (e) => e.target.select() });
      const el = h("div", { class: "callout ok hidden" }, h("div", { class: "field", style: "margin:0" }, h("label", null, label), inp,
        h("div", { class: "form-actions", style: "margin-top:8px" }, h("button", { type: "button", class: "btn btn-sm btn-primary", onClick: async () => toast((await copyText(inp.value)) ? t("Link copied") : t("Copy failed"), "success") }, icon("copy"), t("Copy link")))));
      return { el, show(url, text) { inp.value = url; el.querySelector("label").textContent = text || label; el.classList.remove("hidden"); } };
    };

    // ---- invites
    const inviteEmail = h("input", { type: "email", placeholder: t("name@example.com"), autocomplete: "off" });
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
        if (inviteSend.checked && email) toast(r.emailed ? t("Invite emailed to {email}", { email }) : t("Invite created — email not sent (SMTP not configured)"), r.emailed ? "success" : "warn");
        else toast("Invite link created and copied", "success");
        loadInvites(); loadAudit();
      } catch (e) { toast(e.message, "error"); }
    } }, icon("plus"), t("Create invite"));

    const invites = dataTable({ empty: t("No open invites"), columns: [
      { label: t("Invite link"), render: (i) => h("code", { style: "font-size:11px" }, `${window.location.origin}/register?code=${i.code}`) },
      { label: t("For"), render: (i) => i.email || "anyone" },
      { label: t("Admin"), render: (i) => i.is_admin ? tag("admin", "accent") : "—" },
      { label: t("Created"), render: (i) => fmtDateTime(i.created_at) },
      { label: "", render: (i) => h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: async () => {
        try { await api.del(`/api/invites/${i.code}`); toast("Invite revoked"); loadInvites(); loadAudit(); } catch (e) { toast(e.message, "error"); }
      } }, t("Revoke")) },
    ] });

    // ---- users
    const resetLink = linkBox("Password-reset link");
    const users = dataTable({ empty: t("No users"), columns: [
      { label: t("Email"), render: (u) => [u.email, u.id === me.id ? [" ", tag("you")] : null] },
      { label: t("Role"), render: (u) => u.is_admin ? tag("admin", "accent") : "user" },
      { label: t("Discord Signals"), render: (u) => h("input", { type: "checkbox", class: "switch", checked: (u.features || {}).discord_signals === true, title: t("Grant the Discord listener module"), onChange: async (e) => {
        try { await api.post(`/api/users/${u.id}/features`, { feature: "discord_signals", enabled: e.target.checked }); toast(`Discord Signals ${e.target.checked ? "enabled" : "disabled"} for ${u.email}`, "success"); loadAudit(); }
        catch (err) { e.target.checked = !e.target.checked; toast(err.message, "error"); }
      } }) },
      { label: t("Created"), render: (u) => fmtDateTime(u.created_at) },
      { label: t("Last sign-in"), render: (u) => u.last_login_at ? h("span", { title: u.last_login_ip ? t("from {ip}", { ip: u.last_login_ip }) : "" }, fmtDateTime(u.last_login_at)) : t("never") },
      { label: "", render: (u) => h("div", { class: "users-actions" },
        h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: async () => {
          try {
            const r = await api.post(`/api/users/${u.id}/reset`);
            if (r.emailed) {
              toast(`Reset link emailed to ${u.email}`, "success");
            } else {
              resetLink.show(r.url, t("Password-reset link for {email} — single use, expires in 24 h", { email: u.email }));
              copyText(r.url);
              toast("Reset link created and copied", "success");
            }
            loadAudit();
          } catch (e) { toast(e.message, "error"); }
        } }, icon("key"), t("Reset password")),
        h("button", { type: "button", class: "btn btn-ghost btn-sm", title: t("Log this user out of every browser and phone (lost device, leaked cookie)."), onClick: async () => {
          if (!(await confirmDialog({ title: t("Sign {email} out everywhere?", { email: u.email }), body: t("Every session of this user is ended immediately; they sign in again with their password."), confirmText: t("Sign out everywhere") }))) return;
          try { await api.post(`/api/users/${u.id}/sessions/revoke`); toast("Sessions revoked", "success"); loadAudit(); } catch (e) { toast(e.message, "error"); }
        } }, icon("logout"), t("Sign out everywhere")),
        u.id === me.id ? null : h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: async () => {
          if (!(await confirmDialog({ title: t("Delete {email}?", { email: u.email }), body: t("Their area and all its data (webhooks, tokens, logs) are removed. This cannot be undone."), confirmText: t("Delete user"), danger: true }))) return;
          try { await api.del(`/api/users/${u.id}`); toast("User deleted", "success"); loadUsers(); loadAudit(); } catch (e) { toast(e.message, "error"); }
        } }, icon("trash"), t("Delete"))) },
    ] });

    const audit = dataTable({ empty: t("No admin actions yet"), compact: true, columns: [
      { label: t("When"), render: (r) => fmtDateTime(r.created_at) },
      { label: t("Admin"), render: (r) => r.actor_email || "—" },
      { label: t("Action"), render: (r) => ACTION_LABEL[r.action] || r.action },
      { label: t("Target"), render: (r) => r.target || "—" },
      { label: t("Detail"), render: (r) => r.detail || "" },
    ] });

    async function loadUsers() { try { const r = await api.get("/api/users"); users.update(r.users || r); } catch (e) { /* ignore */ } }
    async function loadInvites() { try { const list = await api.get("/api/invites"); invites.update(list.filter((i) => !i.used_by)); } catch (e) { /* ignore */ } }
    const logins = dataTable({ empty: t("No sign-ins recorded yet"), compact: true, columns: [
      { label: t("When"), render: (r) => fmtDateTime(r.created_at) },
      { label: t("Email"), render: (r) => r.actor_email || "—" },
      { label: t("Result"), render: (r) => r.action === "login_ok" ? tag("ok", "ok") : r.action === "login_blocked" ? tag("rate limited", "warn") : tag("failed", "error") },
      { label: "IP", render: (r) => h("code", null, r.target || "—") },
      { label: t("Detail"), render: (r) => r.detail || "" },
    ] });
    async function loadAudit() {
      try { audit.update(await api.get("/api/audit")); } catch (e) { /* ignore */ }
      try { logins.update(await api.get("/api/audit?kind=logins")); } catch (e) { /* ignore */ }
    }

    root.append(
      pageHead(t("Users"), t("Registration is invite-only. Every user gets their own isolated area; admins can invite, grant features and reset passwords.")),
      card({ title: t("Create invite") },
        h("div", { class: "grid grid-2" },
          h("div", { class: "field" }, h("label", null, t("Invitee email (optional)")), inviteEmail, h("div", { class: "field-hint" }, t("Pre-fills the sign-up form; leave empty for an open invite."))),
          h("div", null,
            h("label", { class: "switch-row" }, h("span", null, t("New invite is admin"), h("small", null, t("They can manage users too."))), inviteAdmin),
            h("label", { class: "switch-row" }, h("span", null, t("Email the invite link"), h("small", null, t("Requires SMTP under Settings → Alerts."))), inviteSend))),
        h("div", { class: "form-actions" }, inviteBtn), inviteLink.el),
      card({ title: t("Accounts"), hint: t("Toggle Discord Signals to grant a user the Discord listener module — its navigation, settings and live connection appear only for users you enable it for.") }, users.el, resetLink.el),
      card({ title: t("Open invites") }, invites.el),
      card({ title: t("Admin activity"), hint: t("Recent admin actions: invites, removals, feature grants, password resets, emergency flattens.") }, audit.el),
      card({ title: t("Sign-ins"), hint: t("Every successful, failed and rate-limited sign-in with the client IP (last 100).") }, logins.el),
    );
    loadUsers(); loadInvites(); loadAudit();
    return () => {};
  },
};
