# Security — model, controls, and what to check after a change

## Threat model

The bridge holds broker credentials and can place real orders. Attackers of interest:
anyone on the internet (the webhook endpoint is public by design), a malicious or careless
co-tenant (other users of the same bridge), a compromised execution agent (VPS), a
compromised signal publisher on the marketplace, and someone with the database file.

## Controls

| Area | Control |
|---|---|
| Transport | HTTPS only in production (Render / Caddy); HSTS, CSP with nonces, `X-Frame-Options`, no inline scripts. Agents refuse plain-http bridge URLs. |
| Sessions | HMAC-signed cookie carrying the user id and a **fingerprint of password hash + session salt**; a password change signs every other session out; *Sign out other devices* (Account) and *Sign out everywhere* (Users, admin) rotate the salt and kill every cookie; `HttpOnly`, `Secure`, `SameSite=Lax`; sign-out is POST-only (a `GET /logout` link or a cross-site image changes nothing). |
| CSRF | State-changing requests are refused when `Sec-Fetch-Site` / `Origin` say cross-site; the webhook endpoints are exempt (they carry their own secret). |
| Rate limits | Per-IP sliding windows on login / register / reset / pairing / push; per-account and global failed-login brakes keyed by a hash — the address the account last signed in from is exempt from those two, so a stranger guessing at a (public) marketplace email cannot lock the owner out of the kill switch; `X-Forwarded-For` honoured only from a private-address peer with `NEXUSPRED_PROXY_HOPS`. |
| Webhooks | URL token (128-bit) + optional passphrase compared in constant time; body cap 64 KB; a deeply nested or non-object body is a 400, never a 500; the token index is per workspace. Marketplace subscriptions re-run the signal in the subscriber's workspace with the subscriber's own checks; a publisher sees subscriber counts, never their account routing. |
| Secrets at rest | Broker tokens, API keys, passwords, SMTP / Discord secrets encrypted with Fernet (`NEXUSPRED_ENCRYPTION_KEY` / `SESSION_SECRET`); masked in every API answer; a secret the key cannot read is kept, never overwritten by a save. Switching a login to another broker drops the previous broker's credentials and accounts. |
| Backups | `GET /api/update/backup` (admin, audited) strips the database-stored session secret and the Web-Push private key from the copy and vacuums it — a backup never lets its holder forge cookies or decrypt other tenants offline. While the encryption key still lives *inside* the database the download is refused (409) until `SESSION_SECRET` / `NEXUSPRED_ENCRYPTION_KEY` is set in the environment (startup re-encrypts under it). |
| Tenancy | Every table row and every settings dict is keyed by workspace (`area_id`); routers resolve the area from the session, never from the request body; marketplace views hide accounts both ways; copy followers from other workspaces appear as `subscriber #n` in the publisher's status, events *and* error texts; copy runners run in their own workspace context so a leader login's problems are logged and alerted to the publisher, never to another tenant; a push endpoint stays with the workspace that registered it. |
| Execution agents | Pair with a 15-minute single-use code; receive a token valid for the relay endpoints only; may only relay to Tradovate hosts (allow-list on both sides); an agent's answer is validated (status code range) and bounded (2 MB body, 500-byte error) before it reaches the order log or the event stream. |
| Outbound | Alert webhooks, push endpoints and updater URLs are checked against loopback / private / link-local / CGNAT / documentation ranges (`is_global`) after DNS resolution; Web Push delivery never follows redirects, so a validated endpoint cannot bounce the bridge into an internal address. |
| Database | Parameterised SQL everywhere; single-use codes burned atomically; user deletion cascades through every workspace table. |
| Self-hosting | Installer creates an unprivileged service user, `ProtectSystem`, `ProtectHome`, `NoNewPrivileges`, secrets file mode 640, backups mode 600, Caddy terminates TLS. `git` never runs as root inside the service-writable checkout (a planted hook cannot escalate), and the root-owned `fluxbridge` helper is replaced only by hand after a review. `run.py` still binds `0.0.0.0` by default because Render and Docker need it; the installers pin `HOST=127.0.0.1` behind Caddy — set it yourself for a bare desktop start on a shared network. |
| Administration | An admin cannot mint a login link for another administrator (only the bootstrap admin, user 1, can); reset links are returned to the admin only when they could not be mailed to the user. |

## What to check after a change (reviewer checklist)

- New endpoint: does it call `require_admin` / rely on the auth middleware? Does every DB
  call take the area from `context.get_area()`? Are ids from the body only used *after* an
  ownership check?
- New secret field: added to `crypto.TOKEN_ACCOUNT_KEYS` / `SECRET_KEYS`, to
  `config._TOKEN_SECRETS` / `SECRET_FIELDS` (masking), and handled on save (`********` keeps
  the stored value)?
- New outbound URL: goes through `security.check_outbound_url` or the broker allow-list?
- New background loop: catches everything, logs once per distinct error, never holds a
  lock across a network call it does not own?
- Log lines / events / order-log `raw`: no tokens, keys or passwords.
- Frontend: no `innerHTML` with user data; `h()` builds elements from text.
- Anything that leaves the server (backup, export, agent bundle): does it carry a key?
- Regression tests for this review live in `tests/test_security2.py` and
  `tests/test_review3.py`; keep them green.

## Reporting

Security problems: open a private issue or mail the maintainer; do not post tokens or
database files.
