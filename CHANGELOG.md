# Changelog

All notable changes to nexuspred. Versions follow [SemVer](https://semver.org/).
Bump `VERSION` on every release — the dashboard compares it against GitHub and
shows the **Update** button when a newer version is available.

## 5.0.0-alpha.7
- **Journal back-fill from Tradovate CSV exports** (`app/journal_csv.py`, **Import CSV**
  on the Journal page, `POST /api/journal/import-csv`). Tradovate's API only exposes the
  current session; past days come from the platform's own reports (Reports →
  Performance / Orders → Export). Performance exports (one row per round trip, with
  P&L) are keyed by their fill ids exactly like the API import, so a trade never appears
  twice; Orders / Fills exports are paired FIFO and matched fuzzily (account, symbol,
  side, qty, prices, exit within 5 s) against API-imported trades. Pick the account the
  export belongs to (a configured Tradovate account merges with API data; any other
  label becomes a manual account), the timezone the platform displayed, and an optional
  flat fee per contract and side. Uploads up to 16 MB. Value-per-point table for common
  CME products when the broker's product record is unavailable.

## 5.0.0-alpha.6
- **Trading journal** (new **Journal** page, `app/journal.py`). Executed trades are
  imported from every enabled Tradovate login — fills, Tradovate's own fill pairs (FIFO
  pairing as fallback), fees per fill, contract → product value-per-point, and a daily
  cash-balance snapshot per account — into `journal_trades` / `journal_fills` /
  `journal_snapshots`, keyed by Tradovate ids so re-imports never duplicate. Import runs
  **automatically once a day after the CME close** (default 23:30 Europe/Zurich; time,
  timezone and on/off under Settings → General → *Trading journal*) and on demand with
  **Import now**. Reporting per **day / week / month** in the journal timezone: net
  result hero, win rate, profit factor, average win/loss, expectancy, max drawdown,
  fees, trading days; P&L-per-period columns, equity curve, month calendar heat-map,
  breakdowns by symbol, account, weekday and hour; every chart has a table twin and a
  tooltip. Trades table with **notes and tags** per trade, cursor pagination, CSV
  export, import history. API under `/api/journal/*` (`overview`, `summary`,
  `calendar`, `trades`, `import`, `imports`, `snapshots`, `export.csv`).
  Dependency-free SVG chart kit (`static/js/charts.js`). New dependency: `tzdata`.

## 5.0.0-alpha.5
- **Signals and orders are persisted** (`signal_log` / `order_log` in SQLite,
  `app/history.py`). A deploy or restart no longer wipes the record: the live 200-entry
  buffers are refilled from the tables at startup, writes go through a background writer
  (never on the request path), rows older than `NEXUSPRED_HISTORY_DAYS` (default 90)
  are pruned daily. Signal entries now carry the webhook name. New endpoints
  `GET /api/history/signals` and `/api/history/orders` (cursor-paginated, filters) and
  `/api/history/stats?days=7` (per-day received / executed / errors / skipped / orders).
  Logs page: 7-day summary, **Load older signals** (result + text filter) and an **Order
  history** table.
- **Contract-rollover warning** (`app/rollover.py`). Once a day the bridge parses every
  dated contract in the symbol map (`MNQU6`, `ESZ26`, …), estimates its roll date per
  product family (index: 3rd-Friday expiry; FX: 2 business days before the 3rd
  Wednesday; crypto: last Friday; metals / grains / treasuries: first notice = last
  business day of the previous month; energy: 3 business days before the 25th of the
  previous month) — or takes the exact expiry from a connected Tradovate session — and
  warns `rollover_warn_days` (default 10) ahead and again once it has passed: event log,
  Discord + email (switch **Contract rollover due** under Alerts, one alert per contract
  and stage), and a banner on the Overview with the suggested next contract.
  `POST /api/rollover/check` re-runs it (the symbol-map editor calls it on save);
  `/api/status` carries `rollover`.
- **Secrets encrypted at rest** (`app/crypto.py`, Fernet). Tradovate access/MD tokens,
  the Discord user token, SMTP password, alert webhook URL, webhook passphrase and
  Discord-target secrets are stored as `enc:v1:…` inside `areas.settings`; every caller
  above `app.db` still sees plain values. Key: `NEXUSPRED_ENCRYPTION_KEY` →
  `SESSION_SECRET` → auto-generated key in the DB (a startup warning tells you when the
  weakest option is in use). Existing plain-text secrets are encrypted once on the first
  start after the upgrade. New dependency: `cryptography`.
- **Sign-ins in the audit log.** Every successful, failed and rate-limited sign-in is
  recorded with the client IP (`login_ok` / `login_failed` / `login_blocked`), including
  the auto sign-in after invite registration and password reset. Users → new
  **Sign-ins** card (last 100), a **Last sign-in** column per user (IP on hover), and the
  Admin activity view stays free of them (`GET /api/audit?kind=actions|logins|all`).
- **Installable as an app (PWA).** Web-app manifest with icons (192/512, maskable,
  Apple touch icon), theme colour per light/dark scheme and standalone display, so the
  dashboard installs on phones and desktops from the browser menu ("Add to Home Screen"
  / "Install"). Shortcuts to Overview, Webhooks and Logs. No service worker on purpose —
  the dashboard always loads the freshly deployed modules.
- **CI pipeline** (`.github/workflows/ci.yml`): every push and pull request runs the
  test suite on Python 3.11 and 3.12 (deprecation warnings are errors), byte-compiles
  the app, syntax-checks every ES module and runs `pip-audit --strict` against the
  pinned requirements; the audit also runs weekly. **Dependabot** opens grouped
  weekly PRs for the web stack and monthly ones for the actions.

## 5.0.0-alpha.4
Security hardening release (no functional changes to signals or trading).
- **Session cookies are bound to the password hash**: changing or resetting a
  password invalidates every other session of that user. Existing sessions from
  earlier versions are rejected once — everyone signs in again after this deploy.
- **CSRF protection**: state-changing requests with a foreign `Origin` /
  `Sec-Fetch-Site: cross-site` are rejected (403). The TradingView ingress is exempt.
- **Rate limits** on `/login`, `/setup`, `/register`, `/reset` and
  `POST /api/account/password` (per client IP, plus a global per-IP ceiling); the auth
  pages show a "too many attempts" message. **Request bodies are capped at 256 KB.**
- **Security headers** on every response: Content-Security-Policy with a per-request
  nonce for the two inline scripts (no `unsafe-inline` for scripts), `frame-ancestors
  'none'` + `X-Frame-Options: DENY`, `X-Content-Type-Options`, `Referrer-Policy`,
  `Permissions-Policy`, `Cross-Origin-Opener-Policy`, HSTS behind HTTPS and
  `Cache-Control: no-store` on API/auth responses.
- **SSRF guard** for URLs the bridge POSTs to (Discord alert webhook, custom
  Discord-signal targets): `http(s)` only, no embedded credentials, host must not resolve
  to loopback / private / link-local ranges. Validated on save with a clear error.
- **Privilege fixes**: `POST /api/update/apply` (git reset + restart of the whole
  process) is now admin-only — any invited user could trigger it before. `POST
  /api/settings` can no longer write `webhooks`, `webhook_secret`, `token_accounts` or
  the `discord_*` keys (each has its own validating endpoint) — a user could previously
  plant arbitrary webhook tokens or an unmasked Discord token through it. `POST
  /api/discord/config` and `/test` require the *Discord Signals* entitlement (the
  listener already did, the config/test routes didn't). An invite created for a
  specific email can only be redeemed with that email.
- **Smaller fixes**: auth-exempt paths match exactly (`/loginx` is no longer exempt);
  invite codes / reset tokens are URL-encoded in redirects; the webhook passphrase is
  compared in constant time; `POST /logout` is accepted alongside `GET`.
- **`NEXUSPRED_PUBLIC_URL`** (new, set to `https://bridge.hurenzone.ch` in
  `render.yaml`): the dashboard shows webhook URLs on this origin whichever hostname it
  was opened on (`/api/status` → `public_url`), and emailed invite / reset links are built
  on it instead of the request's `Host` header.
- **Dependencies** bumped to the current releases (all with a clean `pip-audit`):
  FastAPI 0.141.1 / Starlette 1.6.0, python-multipart 0.0.32, Jinja2 3.1.6, httpx 0.28.1,
  pydantic 2.13.5, uvicorn 0.52.4. The previous pins carried multiple published
  advisories (multipart-form DoS in Starlette 0.37 and python-multipart 0.0.9, Jinja2
  sandbox escapes).
- Tests: 32 new (`tests/test_security.py`), 197 total.

## 5.0.0-alpha.3
- **Marketplace: share a webhook with other users.** An admin publishes a webhook
  (Webhooks → Sharing tab: title, description, visibility *everyone* / *selected
  users*, subscriber list with Remove). Other users subscribe on the new
  **Marketplace** page, route it to their **own** trade accounts with a qty
  multiplier and switch it on/off; their subscriptions are listed under Webhooks →
  *Subscribed signals*. Every alert on the published webhook is executed in the
  publisher's area and forwarded to each enabled subscription in the subscriber's own
  area (own Trading switch, symbol map, alerts, logs) — isolated per subscriber, no
  URL/token/accounts exposed in either direction. Test signals forward only on
  request. Unpublishing pauses, deleting removes subscriptions; all of it lands in the
  admin audit log. New table `subscriptions`; settings schema unchanged.
- **Docs:** production runs on the custom domain `https://bridge.hurenzone.ch`
  (README, setup guide, `render.yaml`). Webhook and invite URLs are derived from the
  request host, so no code or settings change was needed.

## 5.0.0-alpha.2
_On `main` since 2026-09-06 (fast-forwarded from branch `v5`). The previous line, 4.11.0,
is preserved on branch `backup/v4.11.0`; database and settings are compatible both ways._

- **New dashboard, build-free.** The single 1,100-line template + `app.js` is replaced
  by ES modules served straight from `/static` (no bundler, no CDN): a design-token
  based shell with **dark and light themes** (follows the OS, toggle persisted), a
  grouped sidebar with a collapsible Settings section, icon rail and mobile drawer, a
  sticky topbar with live-stream / connection / **trading kill-switch** pills, the 🆘
  Flatten-all button and the update badge. Confirmations use a proper `<dialog>`
  instead of `window.confirm`; copy actions use the iOS-safe helper everywhere.
- **Hash router with deep links** — `#/webhooks/<id>`, `#/settings/alerts`, … — so a
  reload lands where you were (v4 always reopened the Dashboard).
- **One SSE connection feeds the whole UI**: events, signals, **orders, session status
  and Discord** signals arrive live; polling is only a periodic reconcile.
- **Webhooks** page: table + detail drawer with tabs (General / Accounts / Alert
  template / Test signal / Danger zone). **Settings** is one page per concern and each
  page posts only its own keys. Auth pages share one base template and stylesheet.
- Same API payloads and all settings keys as v4 — a v4 `fluxbridge.db` works unchanged.

## 5.0.0-alpha.1
Behaviour-preserving backend refactor of 4.11.0, verified by a characterisation test
suite (`pytest`, 150+ tests) written against the unchanged 4.11 code first.
- **Speed / concurrency**
  - Pooled keep-alive HTTP clients (`app/http.py`) — no TCP+TLS handshake per Tradovate
    order, Discord alert or GitHub check any more.
  - Warm request path needs **no SQLite**: one connection per thread, cached
    `user_count` / user / area lookups (auth middleware went from 4 queries per request
    to none); PBKDF2 (login/setup/register/password change) runs in a worker thread.
  - `SessionManager.reload()` is diff-based: unchanged logins keep their session
    (token state, renew lock, contract cache) across health cycles; re-pasted tokens are
    adopted in place. Health loops run all areas concurrently; order cancels,
    liquidations, `/api/positions` and alert channels are gathered instead of looped;
    connection alerts fire as background tasks so SMTP can't stall a health check.
  - In-memory webhook-token index: a TradingView POST is a dict lookup instead of a scan
    over every area's settings.
  - Discord targets that reference a bridge webhook are dispatched **in-process** (same
    202/403 semantics) — no loopback HTTP through `127.0.0.1:$PORT`.
  - Per-trade locks are released after `close_all` / `full_close` (v4 grew them without
    bound).
- **Fixes found by the tests**: `config.load_settings()` handed out shallow copies, so
  creating a webhook in a fresh area appended into `DEFAULT_SETTINGS` itself and leaked
  into every other fresh area; `/setup` could create two admins on racing first-run
  POSTs; malformed webhook payloads returned 500 instead of 400.
- **Structure**: `app/routers/*` (one module per concern), `app/engine/*` (strategy
  handlers), `app/health.py` (background loops), `app/web.py`; `app/main.py` is the app
  factory with a proper lifespan (loops cancelled and HTTP pool closed on shutdown).
- Versioning: PEP 440 pre-release (`5.0.0-alpha.N`); **no GitHub release tags** on this
  branch, so `main` installations never see it as an update.

## 4.11.0
- **Fix: simultaneous TP/management signals no longer race (one getting lost).**
  When two signals for the *same* trade arrived almost together (e.g. two take-
  profit partial-closes), they were processed in parallel background tasks and
  both read the same “remaining quantity” before either wrote it back — so one
  update overwrote the other and only one TP effectively executed. Signals that
  touch the same position are now **serialised with a per-trade lock** (keyed by
  trade_id for TS-Hunter, by webhook+symbol otherwise), so concurrent TPs, SL/TP
  moves and closes apply one after another with consistent state. Signals for
  different trades/symbols still run in parallel.

## 4.10.0
- **Discord auto-trading fixes for the CoSniper/CoLifetime flow.**
  - **No more double orders.** A provider posts a message and then *edits* it (to
    attach a GIF, etc.), which fired the signal twice → two entries. Signals are
    now de-duplicated by message id + content, so an edit of the same signal is
    ignored (a genuinely changed edit still goes through).
  - **Correct position size.** A bracket-strategy entry ignored the signal's
    *Contracts* and always used the webhook's `default_qty` (so `Contracts: 3`
    opened 1). It now uses the signal's contract count, falling back to the
    default only when the signal omits it. (Simple-strategy entries already did.)
  - **SL & TP are actually placed and kept in sync.** CoSniper sends the entry
    with no stop/target, then separate “Stop / target moved” messages. The old
    `move_sl` only *moved an existing* stop and errored on a missing `new_sl`, so
    nothing was ever placed. A new **set-SL/TP** action now looks at the live
    position and places/replaces the **stop and/or target** to match each update —
    handling stop-only, target-only (stop `—`), and repeated moves — on any
    strategy that has an open position.

## 4.9.0
- **🆘 “Flatten all” emergency button in the header.** A one-click kill-switch that
  cancels every working order and closes every open position on **all** trade
  accounts (across every enabled login), regardless of per-account execution
  toggles or webhook routing. It runs **even when the Trading switch is paused**,
  asks for confirmation first, and reports how many positions/orders it handled.
  New `POST /api/flatten-all`; the action is recorded in the admin audit log. On
  phones the button collapses to just the 🆘 glyph.

## 4.8.0
- **Discord signals now actually execute on the routed webhook.** Previously a
  Discord signal was forwarded in its own shape (`event_type`/`side`/prices) but
  the bridge's webhooks expect a TradingView-style payload (`action`/`symbol`/…),
  so every routed signal was rejected with *“Payload missing 'action' or 'symbol'”*.
  The pipeline now **translates** each parsed Discord signal before dispatch:
  - **Entry (BUY/SELL)** → `buy` / `sell` (quantity from the signal's *Contracts*,
    else the webhook's default; entry/SL/TP prices included when present).
  - **“Closed …”** → `close_all` (flattens the symbol on the routed account).
  - **“Stop / target moved”** → `move_sl` (moves the tracked stop; applied on
    **bracket** webhooks, cleanly skipped — no longer an error — on *simple* ones).
  - Trades still only fire when the global **Trading** switch and the webhook's
    per-account toggles are on, and the symbol must be in your symbol map /
    allowed list (the Discord symbol is the root, e.g. `MNQ`).

## 4.7.5
- **Fix “Closed …” signals being flagged as unrecognised.** The parser's
  trade-closed detection required the title to *start* with “Closed”, but the
  provider prefixes it with an emoji (`🔴 Closed MNQ · −83.00 pts`, `⚪ Closed MNQ`),
  so it fell through to “Unrecognised message”. Title-type detection now ignores a
  leading emoji/symbol prefix, the Unicode minus sign (`−`) is parsed correctly so
  a negative P&L keeps its sign, and the P&L points are read from the title
  (`· −83.00 pts`) when there's no dedicated field. Entry and stop/target messages
  are unaffected.

## 4.7.4
- **Discord listener: rock-solid reconnection + accurate status.** Fixes the
  listener showing **Offline** while it was actually connected, and makes real
  reconnects reliable:
  - **Handle session RESUMEs.** discord.py fires `on_resumed` (not `on_ready`)
    after a transient blip, and the old code only marked itself connected in
    `on_ready` — so after the first blip the status stuck on “connecting” and,
    past the health grace, flipped to **Offline** even though the gateway was
    live. Now `on_resumed`, `on_connect`, and any received message all restore the
    connected state. Incoming traffic counts as proof-of-life.
  - **Jittered exponential backoff (3→60 s) between full reconnects**, instead of a
    fixed 5 s retry. Reconnecting a self-bot in a tight loop makes Discord
    rate-limit the token — which *causes* more drops — so the backoff is what keeps
    the connection stable over time.
  - **A rejected token is detected** (Discord 401/403 / login failure): the
    listener shows **“Token rejected”** and backs off hard instead of hammering
    Discord, so one bad token can't spiral into a reconnect storm.
  - Note: each app deploy restarts the listener for a few seconds — that brief
    reconnect is normal and now shows/handles cleanly.

## 4.7.3
- **Alerts “Notify email” now defaults to each user's own address.** It used to
  default to a single hard-coded address for everyone (wrong in a multi-user app).
  New users' areas are seeded with their own email, existing areas are backfilled
  with the owner's email on startup where it was unset, and the field falls back
  to the signed-in user's email whenever it's empty — while any address a user has
  deliberately set is always preserved.

## 4.7.2
- **Fix false “Cannot set properties of null” error when saving Settings.** The
  save handler wrote its “Saved ✓” confirmation to a `#saveHint` element that
  didn't exist, which threw *after* the settings had already been saved — so the
  save actually worked but surfaced a scary error toast. Gave the Save Settings
  button its `#saveHint` span and guarded the write so a missing element can never
  turn a successful save into an error.

## 4.7.1
- **Fix the Discord Live Signal Feed getting stuck on “reconnecting…”.** The SSE
  streams now send a real named **`ping` heartbeat** every 10 s (instead of a bare
  comment), which the client uses to affirm the connection is alive even when no
  signals are flowing, and which keeps intermediary proxies from treating the
  connection as idle. The status indicator is also **debounced** — a normal quick
  auto-reconnect no longer flashes an alarming “reconnecting…”; it only appears if
  the stream is genuinely down for more than a few seconds. Applies to both the
  Discord feed and the new event/signal log stream.

## 4.7.0
- **Live event & signal logs (real-time, no polling wait).** The Logs view now
  streams new entries the instant they happen over **Server-Sent Events**
  (`GET /api/stream`), instead of waiting on an 8 s poll — events and signals
  appear immediately. A slow 20 s refresh remains as a reconcile fallback, and the
  browser's `EventSource` auto-reconnects if the connection drops. The stream is
  scoped to the logged-in user's area, and log delivery is thread-safe (events
  logged from background tasks/health loops are pushed correctly).

## 4.6.0
- **Email delivery for invites & password resets.** Reusing the SMTP settings you
  already configure for alerts, admins can now have the **invite link emailed
  straight to the invitee** (enter their email + tick *Email the invite link*),
  and a **password-reset link is emailed to the user automatically** when an admin
  starts a reset. Both still show the copyable link as a fallback, and both
  degrade gracefully (link only) when SMTP isn't configured.

## 4.5.0
- **Password management.** Every user can now **change their own password**
  (Settings → Account → Change password): verify the current one, set a new one.
  Admins can issue a **one-time password-reset link** for any user (Users table →
  *Reset password*) — the link opens a set-a-new-password page, works once, and
  **expires after 24 h**; completing it logs that user straight in. Resets are
  recorded in the audit log. New `POST /api/account/password`,
  `POST /api/users/{id}/reset`, and the `/reset` page; a `password_resets` table
  backs the tokens.
  - (Session cookies were already HTTP-only, `Secure` on HTTPS, `SameSite=Lax`,
    with a signed 30-day expiry — no change needed there.)

## 4.4.0
- **Admin audit log.** Admin actions — invites created/revoked, users deleted,
  feature entitlements changed, password resets — are recorded with a timestamp
  and the acting admin, and shown under **Settings → Account → Admin activity**.
  New `GET /api/audit` (admin only); stored in a new SQLite `audit_log` table.

## 4.3.0
- **Discord listener health checks + alerts.** The bridge now watches its own
  Discord Gateway connection and **alerts when the listener goes offline and when
  it recovers**, through the same Discord-webhook and email channels as the
  Tradovate connection alerts. A configurable **grace period** (default 90 s,
  Settings → Alerts) means the library's normal transient reconnects don't alert
  — only a sustained outage of a *wanted* connection does. A dedicated 30 s health
  loop keeps detection quick without changing the token-refresh cadence.
  - The dashboard's **Discord listener** tile now shows a distinct red *Offline*
    when the connection is actually down (vs. a transient *Connecting…*).
- **Webhook-failure alerts.** When a signal is received but execution fails
  (rejected order, unmapped symbol, bad payload…), you get an alert naming the
  webhook and the reason, so a silently-dropped signal can't go unnoticed.
- **“Send test alert” button** (Settings → Alerts) fires a test notification on
  every enabled channel and reports which ones it reached, so you can confirm
  Discord/SMTP is wired up correctly. New `POST /api/alerts/test`.
- New alert toggles: *Discord listener offline / online*, *Signal received but
  not executed*, and the *Discord health grace period*.

## 4.2.0
- **Per-user feature entitlements (admin-managed).** Modules can now be switched
  on/off **per user** by an admin. The first module gated this way is **Discord
  Signals**: under **Settings → Account**, each user row has a *Discord Signals*
  toggle. When off, that user never sees the Discord navigation, the Discord
  Listener settings sub-page, or a live Gateway connection — the listener
  supervisor stays idle for their area regardless of their own settings, so the
  entitlement is enforced on the backend, not just hidden in the UI.
  - Stored per area in SQLite (new `areas.features` JSON column, auto-migrated).
    Existing deployments keep **every feature on** so nothing is lost; brand-new
    invited users start with Discord Signals **off** until an admin grants it.
    The bootstrap admin's own area has all features on.
  - `GET /api/me` now returns the caller's effective `features`; `GET /api/users`
    returns `{users, features}` with each user's flags; `POST
    /api/users/{id}/features` `{feature, enabled}` toggles one (admin only).
- **Navigation: “Account & Users” renamed to “Account.”**
- **“Updates” is now admin-only.** The Settings → Updates sub-page (version /
  update button) is hidden for non-admin users.

## 4.1.2
- **Fix “Create invite” being blocked by the host WAF.** The invite request sent
  a JSON body with an `is_admin` key, which Render's WAF blocks as a suspected
  privilege-escalation attempt — the POST never reached the app and came back as
  an HTML *“Blocked”* page. The dashboard now sends the flag under a neutral
  `elevated` key; the server accepts `elevated` (and still falls back to the
  legacy `is_admin`). This is what actually broke invite creation on the live
  deploy; 4.1.1 only made the failure visible.

## 4.1.1
- **Fix “Create invite” showing an empty block.** The generated invite link is
  now rendered in a selectable, read-only input (tap to select) instead of a
  tiny inline `<code>` element that could render near-invisibly on some phones.
  Added a **Copy link** button with an iOS-safe clipboard fallback
  (`execCommand("copy")` via a hidden textarea when the async Clipboard API is
  unavailable), and an inline error line so failures are never silent. If the
  server response omits the URL, it is reconstructed client-side from the invite
  code.

## 4.1.0
- **Mobile-friendly dashboard** (tested at iPhone Pro Max width, 440px). No more
  horizontal page scroll on phones:
  - Every data table is wrapped in a horizontal-scroll container, so wide tables
    (Connection Health, Active Trades, Recent Orders, …) scroll inside their card
    instead of overflowing the page.
  - Status tiles reflow to a 2-up grid; topbar, forms, URL boxes and code blocks
    adapt; safe-area insets for the notch / home indicator (`viewport-fit=cover`).
  - The expandable **Webhooks** / **Discord channels** rows: heavy summary
    columns (URL, counts) are hidden on phones, and the expanded edit panel no
    longer inherits the table's nowrap — its inputs now fill the card cleanly.
  - Tapping the **Settings** group in the mobile drawer expands its sub-pages
    without closing the drawer, so they're reachable.

## 4.0.1
- **Fix:** health check / token refresh logged `module 'app.state' has no
  attribute 'sessions'` and showed accounts as *0/1 connected* even when the
  token renewed fine. `tradovate` referenced the module-level `state.sessions`
  dict that was removed in the per-area refactor; replaced with a
  `state.has_session()` helper so connection status + lost/restored alerts work.

## 4.0.0
- **Multi-user with isolated areas — replaces Google login.** Fluxbridge is now a
  multi-tenant app: every user signs in with **email + password** and gets their own
  fully **isolated area** (token accounts, webhooks + their URL tokens, Discord
  listener, symbol map, alerts, logs, and live Tradovate sessions are all private).
  - **Invite-only.** First run sends you to `/setup` to create the first **admin**;
    admins create/revoke **invite links** and manage users under **Settings → Account
    & Users**. New users register via an invite and get their own area.
  - **SQLite** persistence (`<data>/fluxbridge.db`: users, areas, memberships,
    invites) — stdlib only, no new dependency. Passwords are salted **PBKDF2**;
    the login session is a signed, HTTP-only cookie. Any pre-existing single-user
    `data/settings.json` is migrated into the first admin's area on setup.
  - **Per-area runtime.** Settings, in-memory logs/signals, Tradovate `SessionManager`s,
    the Discord listener/SSE hub, and active-trade tracking are all keyed per area via
    a request/task **area context**; the health loop and Discord supervisors run per
    area; an inbound `/webhook/<token>` is routed to whichever area owns that token.
  - Removed **Sign in with Google** and the `dashboard_password` / `DASHBOARD_PASSWORD`
    fallback. (Shared areas between users are planned for a later release; the data
    model already carries areas + memberships for it.)

## 3.0.0
- **Renamed to Fluxbridge.** New display/brand name across the dashboard, login
  page, window title, and alerts. (Internal repo name, package, and env vars —
  `NEXUSPRED_DATA_DIR`, `NEXUSPRED_BRANCH`, the `tobiasgiger/nexuspred` repo —
  are unchanged, so existing deployments keep working.)
- **Sign in with Google (email allowlist) replaces the password.** When a Google
  OAuth **Client ID + secret** and at least one **allowed email** are set (Settings
  → Security, or `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_ALLOWED_EMAILS`),
  the dashboard requires Google sign-in and only allowlisted emails get in.
  - Standard OAuth Authorization-Code flow (`/login`, `/auth/login`,
    `/auth/callback`, `/auth/logout`); state (CSRF) + a signed, HTTP-only session
    cookie (no server-side store, no new dependency).
  - Settings → Security shows the exact **redirect URI** to register, plus a
    **Public URL** override for proxied deploys. A **Sign out** button appears when
    Google login is active.
  - The `dashboard_password` / `DASHBOARD_PASSWORD` is now a **fallback** only —
    used until Google login is fully configured — so you can't get locked out.
  - `/webhook/<token>` and `/healthz` remain unauthenticated.

## 2.11.0
- **No-install token grab: bookmarklets.** The Tools tab now offers draggable
  **Discord** and **Tradovate** bookmarklets — drag to the bookmarks bar, click on
  the site to copy the token, no extension install. (Discord's CSP can block its
  bookmarklet; the extension remains the fallback.)
- **Extension prepped for a store listing.** Added PNG icons (16/48/128) and
  `action.default_icon`, a privacy policy (`PRIVACY.md`), and a Chrome Web Store
  submission checklist (`STORE.md`) with listing copy + permission justifications
  and an honest note that a token extractor is likely rejected from the public
  store (unpacked/Unlisted is the practical route).

## 2.10.0
- **Browser Token Extractor available in-app.** The Tools tab now has a
  card that **downloads the extension as a .zip** (`GET
  /api/extension/token-extractor.zip`) and walks through installing it
  (Load unpacked) and using it — no need to clone the repo to get the helper.

## 2.9.0
- **Discord target = pick an existing webhook.** A channel's target is now chosen
  from a **dropdown of the bridge's own webhooks** (the signal is posted to that
  webhook's URL and flows into your strategy routing), with a **Custom URL…**
  option for external targets (URL + optional `X-Webhook-Secret`). Webhook targets
  are stored by id and resolved to the local URL at send time, so regenerating a
  webhook token keeps working. No manual URL/secret typing for the common case.
- **Removed the per-account "Execution On/Off" from Settings.** Which accounts a
  signal trades is decided **per webhook** (Webhooks tab); the global toggle was
  redundant (the routing path already ignored it) and confusing. Settings →
  Tradovate Accounts now shows a **read-only Discovered Accounts** list (Login ·
  Account · Env · Status). Discovered accounts are simply made routable; the
  login-level Enabled switch still disables a whole login. Dashboard stat relabelled
  *Trade accounts* (connected/total).

## 2.8.0
- **Left sidebar navigation + Settings sub-pages.** The top tab strip is replaced
  by a collapsible left sidebar, grouped into Monitoring (Dashboard, Discord,
  Logs), Routing (Webhooks), Configuration (Settings), Tools and Help.
  - **Settings is now an expandable nav group** with one sub-page each:
    General & Trading, Tradovate Accounts, Symbol Mapping, Discord Listener,
    Alerts, Security, Updates — only one shows at a time (no more long scroll).
  - The sidebar **collapses to an icon rail** (state remembered per browser) and
    becomes an off-canvas **drawer with a hamburger** on narrow screens.
  - Added a small inline SVG favicon.
  - Front-end only — no backend/API changes.

## 2.7.0
- **Dashboard restructure & UI cleanup — table-first, consolidated settings.**
  - Tabs reorganised to **Dashboard · Webhooks · Discord · Logs · Settings ·
    Tools · Guide**. Configuration now lives entirely under **Settings**
    (Connection, Trading Rules, Security, Updates, Alerts, Tradovate Token/Trade
    Accounts, Symbol Mapping, and the new **Discord Listener** section). The
    Discord tab is now the live signal feed + status only; its channel/token/
    dry-run config moved into Settings → Discord Listener.
  - **Test & Webhook** and **Simulator** merged into a single **Tools** tab
    (webhook test, Discord test-inject, and the trade simulator).
  - **Stat tiles → slim status bar.** Dashboard and Discord open with a compact
    status strip instead of large tiles.
  - **Webhooks and Discord channels are now expandable tables** — one compact
    row per item (toggle, name, strategy/targets, URL); click a row to expand its
    full settings inline, instead of tall stacked cards.
  - No backend/API changes — same endpoints, same behaviour; purely a
    presentation reorganisation.

## 2.6.0
- **New module: Discord signal listener** (`app/discord_signals/`). Watches one
  or more Discord channels over the **Gateway** (WebSocket push, not polling)
  using a personal user token (self-bot, via `discord.py-self`) and fans parsed
  signals out to configurable webhook targets — typically the bridge itself, but
  any URL works. Runs **inside** the existing FastAPI process (same server, port,
  auth and deploy), as an isolated supervisor task so a Discord failure can never
  crash order execution.
  - **Parser** recognises the three provider embed types (entry, stop/target
    moved, closed). Unknown formats are surfaced as "unrecognised" in the live
    feed and event log — never silently dropped — so provider format changes are
    noticed immediately.
  - **Per-channel → multiple webhook targets**, each with a label, URL, optional
    secret (sent as `X-Webhook-Secret`) and on/off toggle. Enabled targets are
    POSTed **in parallel** (own HTTP client, independent of the Discord client),
    each with its own 5s timeout and isolated error handling.
  - **Live config**: channels, targets and the global **dry-run** switch are read
    per event, so changes on the new **Discord Signals** dashboard tab take
    effect without a restart. Dry-run parses + displays but sends to no webhook.
  - **Live dashboard** via Server-Sent Events (no polling): incoming signals,
    per-target success/failure, latency, and unrecognised raw messages.
  - **Test button** pushes a synthetic embed through the full pipeline to verify
    fan-out, disabled targets, the secret header and dry-run without a live
    Discord connection. Measured signal→dispatch latency is well under the 250 ms
    target (gateway push + parallel send).
  - `discord.py-self` is imported lazily; the bridge still boots and the module's
    parser/config/test work even if it isn't installed (the tab shows "No
    library").

## 2.5.1
- **TS-Hunter: removed the TP2 move-to-break-even.** A partial close still
  resizes the stop to the new remaining quantity every time, but the stop's
  price is no longer moved to break-even at `lifecycle_stage: "TP2"` — it
  stays wherever it was set at entry (`sl.value`) throughout the trade.

## 2.5.0
- **New strategy type: TS-Hunter**, selectable when creating/editing a webhook.
  Matches the TS-Hunter Pine strategy's own alert contract
  (`contract_version: at_execution_command_v5`) directly — no payload
  reshaping needed on the TradingView side.
  - `event: "signal"` opens a Market entry sized from `risk.value` contracts
    (× account multiplier) with a protective Stop at `sl.value`.
  - `event: "management"` / `action: "partial_close_percent"` market-closes
    `percent`% of whatever remains *right now* (not of the original size) —
    three TP hits at 25% / 33.33% / 50% of a 4-lot correctly leave 3 → 2 → 1
    (a "runner"). Every partial close resizes the stop to the new remaining
    qty; when `lifecycle_stage` is `TP2` the stop is also moved to
    break-even (the entry's `tv.entry_price`).
  - `event: "management"` / `action: "full_close"` cancels working orders and
    liquidates whatever remains, regardless of tracked quantity — including
    a safe fallback if the bridge restarted and lost track of the trade.
  - Trades are correlated by the payload's own `trade_id`, not symbol, so
    several concurrent TS-Hunter trades on the same symbol never collide.
  - The Webhooks tab's copy-paste alert template becomes a read-only
    reference for this strategy (the Pine script already generates the exact
    JSON — there's nothing to hand-edit).

## 2.4.0
- **Alerts** (new Settings card): Discord webhook and/or email notifications,
  each channel and each trigger independently toggled.
  - **Connection lost** — which account and broker, sent to Discord + email.
  - **Connection restored** — sent to Discord + email.
  - **Trade executed** — which accounts and strategy, sent to Discord only.
  - Discord messages can tag `@everyone`; email goes out via SMTP (defaults
    to Gmail — use an App Password, not your login password). A failed send
    is logged and never breaks a health check or a trade.
  - Connection lost/restored is edge-triggered (fires once on the actual
    transition, never on the first observation or while state is unchanged).
- **UI**: expanding one card in a two-column row (e.g. Settings → Connection)
  now expands its row-mate too, instead of leaving it collapsed-but-stretched
  and empty-looking. Header buttons/switches (e.g. "+ Add account",
  "Discover / Refresh") are now grouped flush right next to the
  expand/collapse chevron instead of floating mid-row. Added breathing room
  below "Save Settings" and other form-action rows.

## 2.3.1
- Scope the collapsible-cards treatment (2.3.0) to just **Webhooks**,
  **Settings** and **Setup Guide** — the tabs with several stacked cards.
  Monitor, Logs, Test & Webhook and Simulator go back to always-open cards.

## 2.3.0
- **Collapsible cards, collapsed by default.** Every card across all tabs
  (Monitor, Webhooks, Settings, Logs, Test & Webhook, Simulator, Setup Guide)
  is now an accordion you expand by clicking its header — a much shorter page
  to scan. Small stat tiles, the guide's part dividers, and its intro/TOC card
  are left as-is (nothing to collapse). Clicking a table-of-contents link
  auto-expands the card it jumps to. Buttons, toggles, and inputs inside a
  card header (e.g. "Refresh", the webhook Enabled switch) still work
  normally and don't trigger the collapse.

## 2.2.1
- **Ready-to-paste alert template** below each webhook's URL (Webhooks tab and
  Test & Webhook tab): TradingView JSON built from its own placeholders
  (`{{strategy.order.action}}`, `{{strategy.order.contracts}}`, `{{ticker}}`,
  `{{strategy.order.price}}`) matching that webhook's strategy — `simple` gets
  action/symbol/qty, `bracket` gets action/symbol/entry plus sl/tp1/tp2/tp3
  placeholders to fill in from the strategy's own levels. One click to copy.

## 2.2.0
- **Multi-webhook routing, one URL per strategy.** New **Webhooks** tab:
  create/edit/delete a dedicated `/webhook/<token>` per strategy, each with its
  own routed trade accounts (picked from the accounts discovered under
  Settings → Trade Accounts) and its own per-account qty multiplier — signals
  from one strategy never cross into another's accounts.
- Two selectable strategy types per webhook: **simple** (buy/sell the qty from
  the payload, or the webhook's default — no TP/SL, just execution) and
  **bracket** (the existing entry + tp1/tp2/tp3/sl flow, with per-webhook
  default/TP qty). A small strategy dispatch, so future logic (e.g. TP/SL
  expressed in points off a close price) can be added later without touching
  routing.
- Existing installs auto-migrate on first startup: the old `webhook_secret` +
  every currently-enabled trade account become a "Default" webhook (strategy
  `bracket`), so existing TradingView alerts keep working unchanged.
- The Test & Webhook tab gained a webhook picker so test signals run through a
  specific webhook's routing; `/api/webhook-test` is replaced by
  `/api/webhooks/{id}/test`.

## 2.1.0
- **Multiple trade accounts per login, with per-account execution on/off.** One
  Tradovate access token often grants access to several trade accounts. Click
  **Connect & Verify** (or *Discover / Refresh*) and the bridge now lists **every**
  account under each login in the new **Settings → Trade Accounts** card. Switch
  execution on/off per account and set a per-account **Qty ×** — each signal fans
  out to exactly the accounts you switched on.
  - The Monitor header now shows *Logins connected* and *Accounts executing*.
  - Newly discovered accounts default to **off** (except the very first on a fresh
    login), so an account never starts trading without an explicit opt-in.
  - Existing single-account setups keep working unchanged until you refresh.

## 2.0.1
- **Fix: dashboard buttons dead after the v2.0.0 upgrade** (e.g. "+ Add account"
  did nothing). The browser was serving the cached v1.5.0 `app.js` against the new
  HTML. Static assets are now cache-busted with `?v=<version>`, so the dashboard JS
  and CSS always match the deployed version. (If you still see it, hard-refresh once.)

## 2.0.0
- **Token-only, multiple Tradovate accounts** (breaking change). Username/password
  login is removed entirely — there is no more single-login "Accounts" model. Each
  account is now its own session authenticated by its **own access token**, configured
  under **Settings → Token Accounts** (Name, Environment, Access token, optional Check
  token, Enabled, Qty × multiplier).
- Every signal fans out to **all enabled accounts in parallel**; each account resolves
  its own contract, places its own bracket, and tracks its own SL/TP order ids.
- Per-account **token refresh & health**: each token is renewed independently
  (access token → check token, no password fallback) and persisted best-effort so it
  survives redeploys. The Monitor shows one status row per account.
- Removed the `TRADOVATE_ACCESS_TOKEN` / `TRADOVATE_CHECK_TOKEN` /
  `TRADOVATE_USERNAME` / `TRADOVATE_PASSWORD` (and `CID/SEC/APP_ID/DEVICE_ID`) env
  vars and the single-login `/api/accounts` endpoints. New `/api/token-accounts`
  manages per-account tokens (secrets masked on read, merged on save).
- **Migration**: re-add each account under Settings → Token Accounts with its access
  token; old credential settings are ignored.

## 1.5.0
- **No more TradingView timeouts on alert bursts**: the webhook now acknowledges
  instantly (HTTP 202) and processes the signal in the background, so many alerts
  firing within milliseconds are handled concurrently instead of blocking.
- **Parallel account execution**: orders for all enabled accounts are placed
  simultaneously (`asyncio.gather`) instead of one-by-one; within an account the
  TP/SL bracket is also placed in parallel. move_sl / trail_active / close_all
  fan out across accounts in parallel too.
- **Contract resolution cached** (1 h) so bursts don't repeat `/contract/find`.

## 1.4.6
- **Break-even = entry price**: a TP1 `move_sl` (or any "breakeven" message) now sets
  the stop to the original **entry price** of the initial buy/sell signal, instead of
  the signal's `new_sl` (which is net-of-fees and slightly off). Trailing `move_sl`
  updates still use `new_sl`. Toggle via *Trading Rules → “Break-even = entry price”*.

## 1.4.5
- Removed the **Open P&L** column from Open Positions. Tradovate's position feed
  has no live P&L (it needs a market-data subscription), so it only ever showed
  0.00 — the column now shows Symbol / Net Pos / Avg Price instead.

## 1.4.4
- **Tokens survive redeploys**: the renewed token persisted on disk now wins over a
  stale `TRADOVATE_ACCESS_TOKEN` env var (the env token is only a seed and expires).
  The loader picks whichever token has the later expiry.
- **Credentials via env vars**: `TRADOVATE_USERNAME` / `TRADOVATE_PASSWORD` (and
  optional `TRADOVATE_CID/SEC/APP_ID/DEVICE_ID/ENVIRONMENT`) — set once on the host
  and the bridge logs in fresh after every deploy, no manual token entry.

## 1.4.3
- **Fix `move_sl` 400 error**: `/order/modifyorder` now sends the required
  `orderQty` and `orderType` (it was failing with “missing required field orderQty”).
- **Stop-loss size now tracks the remaining position**: after TP1 the SL shrinks to
  2 contracts, after TP2 to 1 (scaled by each account's multiplier). The remaining
  qty is derived from the signal's event (`tp1_hit`/`tp2_hit`); `trail_active` (TP2)
  also resizes the stop.

## 1.4.2
- **Proactive token refresh** (adopted from Bridge-Bot-TV): the background loop now
  force-renews the token *before* it expires — at least 5 min ahead and at least
  every 25 min — instead of waiting for it to lapse, with a 60 s retry on failure.
  Adds `proactive_refresh()` and expiry-aware `seconds_until_refresh()`.

## 1.4.1
- Added a **standalone Setup Guide page** (`docs/setup-guide.html`, self-contained,
  inline styles) served at **`/guide`** (public, auth-exempt) with a link from the
  dashboard's Setup Guide tab.

## 1.4.0
- Reworked the in-dashboard **Setup Guide** into a structured how-to (Parts A–H):
  Render deploy, self-host on Linux, configure, TradingView, test, go-live,
  operate/update, and troubleshooting — with sub-steps throughout.
- **Open Positions** now shows the resolved contract symbol (not the numeric id)
  and lists only *open* positions (netPos ≠ 0); flat/closed ones are hidden.
- **Token auto-renewal hardened**: persisting a renewed token is now best-effort,
  so a read-only data dir can no longer break renewal and drop the session
  (caused the "disconnected, signal didn't go through" issue).

## 1.3.3
- Don't 500 when `NEXUSPRED_DATA_DIR` isn't writable (e.g. Render env var set but
  no persistent disk mounted): fall back to the local `data/` dir with a clear
  warning instead of crashing on save.

## 1.3.2
- Pin **Python 3.11** via a `.python-version` file so Render (and other hosts)
  don't pick Python 3.14, which has no prebuilt wheels for `pydantic-core`/`orjson`
  and fails the build trying to compile them. Build-troubleshooting notes added.

## 1.3.1
- `runner_exit` signal (action `close_all`) — already handled by the action-based
  router; added a Test & Webhook preset and a Simulator scenario for it.

## 1.3.0
- **Render.com deployment** for TradingView's port-80/443 requirement: added
  `render.yaml` blueprint (web service + persistent disk), `NEXUSPRED_DATA_DIR`
  to store settings/tokens on a mounted disk, an unauthenticated `/healthz`
  probe, and a Render walkthrough in the Setup Guide + README.
- **Dashboard auth**: optional HTTP Basic auth via `DASHBOARD_PASSWORD` env var
  or the new *Dashboard password* setting — protects the dashboard + API on
  public hosts; `/webhook/<secret>`, `/static`, `/healthz` stay open.
- Self-update button now reports that managed hosts (Render) deploy via git push.

## 1.2.3
- Setup Guide: added a **Quick install** copy-paste block (apt → git clone →
  install → service → firewall), a dedicated **Open / whitelist port 9000** step
  (ss check, ufw/firewalld/iptables, cloud security groups, curl test), and a
  stronger **keep running after SSH disconnect** step (systemd + enable-linger,
  tmux, nohup). Troubleshooting updated.

## 1.2.2
- Rewrote the in-dashboard **Setup Guide** as a beginner-friendly, 15-step
  walkthrough with copy-paste **Linux** commands (using `/home/py/nexuspred`):
  prerequisites, git clone, install, start, run-on-boot (systemd + linger),
  open dashboard, authenticate, connect/accounts, symbol mapping, safe testing,
  exposing to TradingView (Cloudflare Tunnel/ngrok), go-live, updates, and a
  troubleshooting section.

## 1.2.1
- Add `connect-git.bat` / `connect-git.sh` to turn a ZIP-downloaded folder into a
  Git checkout so the dashboard **Update** button works; clearer "not a git
  checkout" message pointing to them.

## 1.2.0
- **Current Symbol Mapping** card in Settings: map each TradingView symbol to the
  exact Tradovate contract (e.g. `MNQ1!` → `MNQU6`) and edit it on rollover.
  Seeded with NQ/MNQ/ES/MES (U6) and GC/MGC (M6).

## 1.1.0
- **Multi-account routing**: enable multiple Tradovate accounts; every signal is
  sent to all enabled accounts (with per-account quantity multiplier).
- **Auth like Bridge-Bot-TV**: `TRADOVATE_ACCESS_TOKEN` / `TRADOVATE_CHECK_TOKEN`
  env vars, JWT-`exp` expiry, renew chain access → check token → credentials login,
  web-trader fallback (no API subscription). OAuth removed.
- **Trade simulator** tab and **connection health** monitoring.
- **Fix**: contract resolution no longer fails with `404 /contract/find`
  (falls back to `/contract/suggest`, front-month selection).
- **Fix**: market entry orders no longer send a `price` (Tradovate rejection).
- Self-updater hardened; default port changed to 9000.

## 1.0.0
- Initial release: TradingView webhook → Tradovate bridge, dark dashboard,
  order logic (market entry + TP limits + SL stop, move_sl, close_all),
  installers, Setup Guide, and GitHub auto-updater.
