# Changelog

All notable changes to nexuspred. Versions follow [SemVer](https://semver.org/).
Bump `VERSION` on every release — the dashboard compares it against GitHub and
shows the **Update** button when a newer version is available.

## 5.0.0-alpha.69
- **Five fixes by andrasmining merged** (PRs #13–#17). A TS-Hunter entry whose trade id is
  already tracked is skipped instead of overwriting the first position's stop tracking;
  a set_sl_tp that could not place the protective orders reports `error` instead of "no
  open position"; the bracket's remaining quantity follows the broker's stop modification
  instead of preceding it; journal CSV exports neutralise spreadsheet formula prefixes;
  a registration that loses the single-use invite race no longer leaves an orphaned
  account behind (now combined with the enforced two-factor enrolment).

## 5.0.0-alpha.68
- **Two-factor authentication.** New accounts (first-run setup and invite sign-up) must
  enrol with an authenticator app before they can use the dashboard: QR code / key,
  6-digit confirmation, then ten single-use backup codes shown once. Signing in asks for
  the code after the password; a backup code works instead of the app. Settings → Account
  shows how many backup codes are left and issues a fresh set of ten on request (password
  + current code; the old set stops working); accounts that enabled 2FA voluntarily can
  disable it there, sign-up accounts cannot. Admins reset a user's 2FA on the Users page
  (lost phone and codes) — secret and codes are wiped, every session ends, the user enrols
  again; a password reset does the same. TOTP codes are single-use, the second-factor
  page is rate-limited, everything is audited, the secret is stored encrypted. New
  dependency: `segno` (QR code, pure Python).

## 5.0.0-alpha.67
- **German and English dashboard.** Every page, menu, dialog, toast, hint and the
  sign-in / setup / reset pages are localised (1 000+ strings). The language follows the
  browser by default; Settings → General & Trading → Display → Language forces *Deutsch*
  or *English* per workspace (the page reloads; sign-in pages follow via cookie). Dates
  and numbers use the chosen language's format. English remains the source text in the
  code; the German dictionary is `static/js/locales/de.js` (+ `app/i18n.py` for the auth
  pages), a missing entry falls back to English and a test guards coverage. Log events,
  alerts and API errors stay English.

## 5.0.0-alpha.66
- **Close failures are never reported as clean** (PRs #11 and #12 by andrasmining,
  merged). A working order that survives both cancel attempts after a close makes that
  account fail (it stays tracked for a retry, the alert is unchanged) instead of returning
  success with a stop or target still resting on a flat position. Close results carry
  `status: error` whenever an account failed, so the signal log and history agree with
  the broker outcome. The error line now says which case it is: "position closed but its
  orders are not" versus "the position may still be open".

## 5.0.0-alpha.65
- **Calendar shows this and next week.** The weekly file stops at Sunday, so the coming
  weeks (15 days) are previewed from TradingView's public calendar endpoint and marked
  *preview* until the weekly file delivers them on Sunday evening — then its rows replace
  the preview. The default range is *This & next week* (Monday to the following Sunday);
  the news lock also sees preview events. Filters are compact toggle chips on one row
  each instead of checkbox boxes.

## 5.0.0-alpha.64
- **Broker-neutral wording.** Texts that applied to every broker but said "Tradovate"
  are generic now: the logins card (per-broker instructions for Tradovate, Rithmic,
  ProjectX), Overview P&L placeholder, symbol mapping ("Broker contract", Tradovate form
  with translation for the other brokers), Simulator, Flatten-all confirmation, sidebar
  tagline, copy-group feed-loss hint, connection alerts (name the login's broker), copy /
  risk-guard messages ("no broker account id"). Features that really are Tradovate-only
  say so: journal import (other brokers via CSV), execution agents, the token extractor.

## 5.0.0-alpha.63
- **Broker Accounts.** The menu entry, page and every hint now say *Broker Accounts*
  instead of *Tradovate Accounts* — the page manages Tradovate, Rithmic and ProjectX logins.
- **Calendar fixes.** The currency and impact filters rendered as
  `[object HTMLLabelElement]` text instead of checkboxes; they are checkboxes again. An
  empty range now explains itself: the feed publishes one week at a time, so on a weekend
  "Next 7 days" says up to which day the calendar is covered and that next week's events
  arrive on Sunday evening.

## 5.0.0-alpha.62
- **Close tracking per account** (PR #10 by andrasmining, merged). A `close_all` /
  TS-Hunter `full_close` with a mixed outcome keeps the accounts whose close failed in
  the trade record and retries only those — a failed account is never forgotten while it
  still holds a position, and accounts that already closed are not flattened again.
- **Untracked positions are still closed.** With a trade record present, accounts that
  are enabled on the webhook but not in the record (an entry whose broker answer was lost,
  or a position opened by hand on a routed account) are checked at the broker and closed
  when they hold the contract; flat accounts get no liquidate call. Close results now
  carry a `failed` list.

## 5.0.0-alpha.61
- **Performance pass** (see `docs/PERFORMANCE.md` for the numbers). Settings reads hand
  out a pickled snapshot instead of a Python deep copy (135 → 25 µs on a normal workspace,
  583 → 70 µs on a large one); a webhook signal reads the settings once instead of 3–4
  times, and the per-order risk check / news lock read a single key. Each workspace keeps
  its own P&L polling cadence (a busy dashboard no longer drags idle workspaces to a 5 s
  broker poll); the health loop renews each login on its own schedule and backs a failing
  login off (60 → 600 s) instead of renewing every login every minute. `/api/positions` and
  the copy-order reconcile fetch one list per login, not per account; contract names are
  cached on the session. Copy-engine event and state rows are written on the history
  writer thread, never on the event loop between two mirror orders. The live stream
  serialises each frame once and tells a slow tab to resync instead of silently dropping
  messages; tables skip the rebuild when rows did not change and the Logs page inserts new
  rows instead of repainting 200. The news lock check is memoised for 2 s and its feed is
  retried every ten minutes after a failure, not on every page view. Benchmark: 885 →
  ≈ 1 200 signals/s.
- **Security patches** (see `docs/SECURITY.md`). The database backup strips the DB-stored
  session secret and the Web-Push private key (and refuses with 409 while the encryption key
  still lives inside the database); Web Push never follows redirects; publishers see
  subscriber counts, never their account routing; the failed-login brakes spare the address
  the account last signed in from (no lock-out of the kill switch by a stranger); reset
  links are returned only when they could not be mailed and only the bootstrap admin can
  reset or sign out another administrator; sessions are revocable ("Sign out other devices"
  on the Account page, "Sign out everywhere" on the Users page); sign-out is POST-only;
  the webhook ingress answers 400 to nested / non-object JSON; agent relay results are
  validated and bounded; a push endpoint stays with the workspace that registered it;
  the self-hosting helper never runs git as root inside the service-writable checkout.
- **Copy trading fixes.** Followers stay within the leader's broker (positions are keyed
  by broker contract ids); follower logins are resolved by login id, not by position;
  runner tasks run in the group's own workspace (a leader login's problems no longer land
  in another tenant's log); an account that leaves a group (unsubscribe, kick, unpublish,
  edit) gets its mirrored working orders cancelled first; a subscriber's account name is
  masked in the publisher's error texts too; the reconcile never re-opens a position the
  news lock flattened, and uses the current leader picture after waiting for the lock;
  the protective stop of a bracket waits out a short 429 penalty instead of failing twice
  within half a second.
- **Rithmic / ProjectX fixes.** Two-digit years (MNQZ26) parse as contracts; cancel and
  modify honour the executor's account (twins reloaded after a restart) and never guess the
  primary account on a multi-account login; one closed account no longer takes the whole
  login's position/order feed down (reported once); a timed-out order is "outcome unknown"
  with an alert, not a rejection, and an OCO never cancels a leg that may be live; replaced
  Rithmic clients and sessions are disconnected; switching a login's broker resets the old
  broker's accounts and credentials; ProjectX shares one account and position list per
  login within a P&L tick and refuses to guess an unknown order's account.
- **Installers.** The server installer keeps operator-added variables in the environment
  file and honours `--port` on re-runs; `runuser`/`sudo`/`su` are used whichever exists;
  restore copies and validates before it swaps the live database; the agent installer
  refuses `sudo` on macOS, allows `--dir` under `/home`, quotes paths; the agent rotates its
  log and waits five minutes on a revoked token instead of restart-looping.

## 5.0.0-alpha.60
- **Calendar page.** The economic calendar has its own page under Monitoring (next to
  Journal): every entry of the feed plus your manual events, grouped by day, with filters
  for range (default next 7 days; today, 3 / 14 / 30 days, past 7 days), currency, impact,
  free text and *lock-relevant only*. Each row shows whether the news lock counts it and
  its lock window. The feed publishes one week at a time; the bridge now keeps past weeks
  for 90 days and shows the covered span. Settings → News & Calendar keeps only the lock
  rules and status.

## 5.0.0-alpha.59
- **ProjectX broker adapter (beta, untested with a real key).** Logins for TopstepX,
  Bulenox, Alpha Futures, Blusky, E8X, Tradeify and the other ProjectX firms (Settings →
  Tradovate Accounts → Broker *ProjectX*: user name, API key, firm). `app/projectx.py`
  implements the broker interface over the Gateway REST API: token login with renewal,
  accounts, positions, working orders with versions, market / limit / stop / stop-limit,
  modify, cancel, flatten, linked OCO legs, contract search with front month and root
  aliases (NQ→ENQ, ES→EP …), realised P&L from today's trades and open P&L from the last
  bar, per-login pacing with 429 back-off. Copy trading polls a ProjectX leader; risk
  guard, sizing, order log and alerts are shared. Executors now hand the account to
  modify / cancel calls for brokers that need it. Tradovate logins are untouched.

## 5.0.0-alpha.58
- **Rithmic broker adapter (beta, untested against a real system).** A login can now be a
  Rithmic login (Settings → Tradovate Accounts → Broker *Rithmic*: user, password, system
  name, gateway). `app/rithmic.py` implements the broker interface over the
  `async_rithmic` Protocol Buffer client: accounts, positions, working orders with
  versions, cash snapshot, RMS rules, front-month lookup, market / limit / stop /
  stop-limit orders, modify, cancel, flatten — with the same order log, risk-guard
  chokepoint and alerts as Tradovate. Copy trading polls a Rithmic leader (no user-sync
  socket), the P&L and risk guard read its snapshots. Rithmic ids (strings) are mapped to
  stable integer ids for the engines. Limits: OCO pairs become two independent orders,
  no execution-agent routing, no journal import, live systems need Rithmic's app
  registration. Tradovate logins are untouched; the package is imported lazily.

## 5.0.0-alpha.57
- **Broker interface (step 1 of the Rithmic plan, no behaviour change).** `app/broker.py`
  defines the surface the bridge may use — `BrokerSession` (login, feeds, contracts) and
  `BrokerExecutor` (orders) — and the Tradovate session implements it with named methods
  (`positions_snapshot`, `orders_snapshot`, `cash_snapshot`, `contract_info`, …). The copy
  engine, P&L, position watch, journal importer and rollover no longer call Tradovate
  endpoints directly; every request still goes through the same paced, prioritised
  Tradovate request path as before. A login whose `broker` is not shipped by this version
  gets a disabled placeholder session and a clear status, so it can never trade by
  accident. Test fakes share one feed mixin.

## 5.0.0-alpha.56
- **Copy groups on the marketplace.** A copy group can be published like a webhook (drawer
  → *Marketplace*: title, description, visibility). Other users see it on the Marketplace
  page and **follow** it with their own accounts and sizing (multiplier / fixed / max /
  direction). The mirror keeps running in the leader's workspace, but every follower account
  trades on its owner's login, under the owner's Trading switch, risk locks, order log and
  alerts; the copy events land in both workspaces. Privacy both ways: followers never see
  the leader's accounts, the leader sees followers as *subscriber #n* and their e-mail in
  the follower list (with a Remove button). Subscribers get a *Following* card on the Copy
  Trading page with the live picture of their accounts, an on/off switch and Manage.
  Unpublishing or unsubscribing removes the accounts from the mirror without closing
  positions. An account may follow one leader only (own groups and subscriptions checked).
- **Rithmic analysis** (no code): `docs/RITHMIC.md` — what a second broker would take.

## 5.0.0-alpha.55
- **Economic calendar and news lock.** New **Settings → News & Calendar**: the weekly
  ForexFactory calendar (no key needed, refreshed every six hours) with the events that
  matter for the workspace (currencies, impact levels) and their lock windows. With the
  lock enabled, no new entry is executed from X minutes before to Y minutes after a
  release — webhooks, Discord signals and marketplace subscriptions alike; closes, stop
  moves and the copy mirror always run. Optional *flatten*: every position of the
  workspace is closed when the window opens (same as the SOS button). Manual events
  (speeches, earnings) share the window. A window that opens is alerted (Discord, push),
  and the top bar shows a red *News lock* pill while it is active.

## 5.0.0-alpha.54
- **Execution agent for Linux / macOS in one line.** **Settings → Execution Agents →
  Linux one-liner** shows a ready-made command with a fresh pairing code;
  `deploy/install-agent.sh` installs Python if missing, downloads the agent, pairs it
  (token in `agent.json`, mode 600, own service user) and installs a sandboxed systemd
  service (macOS: launchd) that starts on boot and restarts on exit. `fluxbridge-agent
  status | logs | restart | update | uninstall` manages it; re-running the line updates the
  agent and keeps the pairing. Agent 1.3.0 adds `--pair-only` for installers.

## 5.0.0-alpha.53
- **Self-hosting in one line.** `deploy/install-server.sh` turns a Debian / Ubuntu server
  into a running bridge: Caddy with automatic HTTPS (TradingView needs a valid certificate),
  a sandboxed systemd service that restarts on crash and reboot, secrets generated once into
  `/etc/fluxbridge/env`, a daily backup timer (14 days kept) and the `fluxbridge` command
  (`status`, `logs`, `update`, `backup`, `restore`, `domain`, `uninstall`). Re-running the
  line upgrades. **Settings → Updates → Download backup** exports the whole database as one
  SQLite file for the move (restore with `fluxbridge restore FILE`). Under systemd the
  one-click updater shuts down cleanly and lets the service restart it. Guide:
  `docs/SELF-HOSTING.md`, including the step-by-step move from Render.

## 5.0.0-alpha.52
- **Journal calendar: weekly totals.** A *Week* column on the right of the calendar sums
  each week's net P&L and trade count (Monday–Sunday within the shown month), coloured like
  the day cells; the tooltip shows the exact figure and the trades. On phones the day and
  week cells show a compact figure (`+1.2k`, `−78`) instead of an ellipsized `+$1…`.

## 5.0.0-alpha.51
- **Copy trading: first follower seed on a freshly booted host.** The reseed throttle
  took "never seeded" (0) for "seeded a moment ago" while the host's monotonic clock was
  still below 60 s after boot, so the first seed — and with it the restored twins — could
  be skipped right after a restart. Seen as a flaky CI failure; fixed with a test.

## 5.0.0-alpha.50
- **Code review, part 2: persistence, security, background loops.**
  - *Settings can no longer be wiped.* A settings save that starts from an unreadable
    database read (disk error, corrupt row) is refused with a 503 instead of writing the
    defaults over the real configuration; a corrupt settings row is an error, not an empty
    area. A stored token the current encryption key cannot read is kept as it is on every
    save (until the key is corrected or the token re-entered) rather than being replaced by
    the empty placeholder. Writes start from the in-memory copy, and the webhook token index
    is rebuilt only when a webhook list actually changed.
  - *Single-use codes are single use under load:* invite codes, agent pairing codes and
    password-reset links are burned atomically, so two racing submits can never both
    succeed. Deleting a user now also removes the workspace's history, journal and
    copy-trading rows. Journal lookups by fill id and by account/date have indexes; SQLite
    runs WAL with `synchronous=NORMAL` (no fsync per commit).
  - *Security:* `X-Forwarded-For` is honoured only when the direct peer can be our reverse
    proxy (private / loopback address); the outbound-URL guard uses the global-address test
    (carrier-grade NAT and documentation ranges are internal too); the login brake keys by
    a hash of the address; a cross-site `GET /logout` signs nobody out; changing your
    password re-issues this session's cookie (other sessions are still signed out).
    Webhook alerts are capped at 64 KB.
  - *Loops:* the P&L poll runs areas in parallel, each with its own 90 s timeout, so one
    slow broker no longer holds another workspace's risk guard; trade-executed alerts are
    sent off the request path (a slow SMTP server no longer delays the webhook answer);
    liquidations reuse the cached contract id; abandoned relay jobs are pruned from an
    offline agent's queue; the push diagnostic runs in a worker thread; drawdown state is
    written to the settings at most every 30 s (flushed at shutdown) instead of on every
    up-tick; a rollover check that failed is retried the same day; webhook edits are atomic
    read-modify-write; stale active-trade records are swept after 14 days; the closed-trades
    list for the daily summary is capped; history write failures are logged; Discord
    listeners start at startup (the call was unreachable).
  - *Dashboard:* the settings form posts nothing but the changed fields (an unchanged
    submit no longer re-posts every value).

## 5.0.0-alpha.49
- **Code review, part 1: the money paths.** A full review of the order, copy-trading,
  risk and P&L code; everything that could place a wrong order, miss one, or double one
  is fixed in this release.
  - *Tradovate:* an order the broker rejects with HTTP 200 (`failureReason` in the body)
    is now an error, not a success — the bridge no longer believes a stop is working when
    it is not. Order, cancel, modify and liquidate calls go through their own fast lane
    (60 ms spacing, never queued behind the P&L poll); a 429 penalty longer than three
    seconds fails them fast instead of blocking. A token that cannot be renewed keeps
    serving while it is still valid; an empty account discovery no longer wipes the account
    list, and a rediscovery keeps each account's enabled flag and risk settings.
  - *Execution agent:* a job the agent claimed but never answered is an **unknown
    outcome** (alerted) rather than a silent failure, and a job the bridge abandoned can
    no longer be picked up by the agent later.
  - *Strategies:* a protective stop that fails to place is retried once and alerted;
    closing a trade cancels its orders, liquidates, then retries the cancels; a stop whose
    quantity reaches zero is retired instead of left working; *set SL/TP* places the new
    order before cancelling the old one, so a position is never unprotected in between.
  - *Risk guard:* a lock never fires twice for the same loss — the 17:00 New York day roll
    no longer re-flattens an account, the lock is written before the flatten starts, and a
    manual unlock sticks for the day.
  - *Copy trading:* the reconcile leaves a follower alone for five seconds after an order
    (the fill is not yet in the broker's position list), holds the follower lock while it
    compares and writes, and never re-enters a contract that was flattened. While the
    socket is synced a REST difference must be seen twice before it is mirrored (a
    snapshot can predate an already applied socket event). A feed-loss flatten closes
    only what the mirror opened, on enabled followers, using the broker's real position.
    A 429 throttle is never a lost feed. A position that appears in the socket's sync
    snapshot after the REST seed is an entry and is mirrored. Twins are cancelled when a
    group is disabled or deleted.
  - *Order mirror:* passes never run concurrently (one twin per leader order, whatever
    the poll and the socket see at the same time); a leader order in transition
    (PendingReplace, PendingCancel, Suspended) keeps its twin instead of cancel + recreate;
    OCO legs that size differently on the follower are mirrored as two independent orders
    (the broker requires one quantity per OCO); a twin whose cancel failed while it is
    still working is kept and retried, never forgotten; a twin that filled at the broker
    while the leader order still works is not re-created for 30 s; socket events that
    arrive during a pass are applied by one more pass; final orders are pruned from the
    socket state.
  - *Dashboard:* the marketplace subscription drawer's sizing columns (Same / Multiplier /
    Fixed / Max) were never wired — subscriptions always traded 1:1 — and the drawer
    crashed on a missing helper; both fixed. Deleting a copy group works again. The event
    stream reconnects with backoff after the browser closed it and refreshes orders and
    logs on resync. Settings save posts only the fields that changed (blank numbers are
    left alone). Save / Connect / Update buttons cannot be double-clicked. P&L paints skip
    stale snapshots.

## 5.0.0-alpha.48
- **Risk guard on the exchange's clock.** A lock now lasts the Tradovate trading day
  (rolls at 17:00 New York), the same day the broker's daily P&L uses — a Zurich-midnight
  day could unlock an account while Tradovate still counted the loss as today's and fire
  again. The flatten time can be set in **New York time** per account (exchange clock,
  unaffected by the weeks Europe and the US disagree on daylight saving) or, as before,
  in the journal timezone.

## 5.0.0-alpha.47
- **Audit fixes, part 3: copy trading.** The mirror state (which contracts are mirrored,
  the leader's last known size) is persisted, so a restart — every deploy is one — carries
  on where it left off: adds during the downtime are mirrored, a leader that went flat is
  closed on the followers, and a running trade is no longer demoted to *baseline*. A
  leader closing a position that was never copied (baseline) is not mirrored any more,
  so a follower's own position in that contract is left alone. A follower account may
  follow one leader only (validated across groups). New per-group choice after a feed
  loss: *flatten followers, then pause* (as before) or *pause only*.

## 5.0.0-alpha.46
- **Audit fixes, part 2: stable login ids.** Every Tradovate login now carries a permanent
  id (`lid`), assigned automatically on the first load after the update. Webhook routes,
  copy-group leaders and followers and marketplace subscriptions are stamped with the id
  of the login they point at and resolved by it — reordering or deleting logins can no
  longer move a route onto a different login (previously routes were addressed by list
  position). Saving the login table matches rows by id, so removing one login never hands
  its token or account list to the next one. Token renewals and re-discoveries write by
  id too; a login deleted in the meantime is never written into another one.

## 5.0.0-alpha.45
- **Audit fixes, part 1.** *Connect & Verify* no longer drops an account's risk rules
  (they now survive re-discovery). Bracket take-profit slices never add up to more than
  the entry (surplus TPs are dropped) — with a fractional multiplier or fixed sizing they
  could open a reverse position. A risk rule keeps the live P&L poll running even when
  `pnl_poll_seconds` is 0. Orders, modifications, cancels and liquidations skip the
  per-login pacing queue and are refused immediately while a 429 penalty runs instead
  of executing a minute late. A bare root (`MNQ`) never resolves to a contract past its
  roll date. Copy-trading reconcile leaves followers locked by the risk guard alone.
  A symbol-scoped close whose working orders carry no contract information now leaves
  them in place with an error event instead of cancelling every order on the account.

## 5.0.0-alpha.44
- **Per-account sizing on webhooks and marketplace subscriptions** (`app/sizing.py`).
  Each routed account picks **Same** (1:1 the signal's contracts), **Multiplier** (signal ×
  factor, rounded half up, never below 1) or **Fixed** contracts (bracket take-profit
  slices scale proportionally), plus an optional **Max** cap — the same rules as copy
  trading, in the Accounts tab of the webhook drawer and the subscription drawer. Older
  entries with only a `qty_multiplier` keep their behaviour. Rounding is now half up in
  every strategy (bracket used to truncate, simple used half-to-even): 3 × 1.5 = 5,
  1 × 2.5 = 3.

## 5.0.0-alpha.43
- **A close can no longer hit the wrong account.** Every account-scoped Tradovate call
  (`liquidateposition`, working-order list, positions, `placeorder`, `placeoco`) fell
  back to the login's *primary* account when the routed account entry carried no
  Tradovate id — so a `close_all` / Discord *close* for account B could flatten account
  A of the same login. Now the id is taken from the call, else looked up by account
  name in the login's discovered accounts, and if it is still unknown the call is
  **refused** with "no Tradovate account id known — run Connect & Verify"; only the
  legacy single-account path (no account named) uses the primary. The order log records
  the account id every order and liquidation went to.

## 5.0.0-alpha.42
- **Far fewer Tradovate requests.** The live P&L poll fetched, every 5 s per login, the
  risk record, the position list and one cash snapshot **per account** — with a dozen
  accounts that alone was ~2.5 requests/s per login and the real reason for the 429s.
  Now: the risk record is cached for 5 minutes, positions are read once per login and
  handed to the position watcher (no second read), and a flat account gets a fresh cash
  snapshot only every 6th tick (accounts with a position, or just closed, every tick). A
  login with 12 idle accounts drops from ~14 to ~1 request per tick.
- **One request budget per login.** `TradovateSession` paces every request (at most 5/s
  per login, one at a time) and remembers a 429 penalty, so every loop that shares the
  login — P&L, health, copy trading, journal, orders — waits it out together instead of
  piling on.

## 5.0.0-alpha.41
- **Copy trading: orders over the socket, REST only as a safety net.** With the user sync
  now answering, the leader's orders are maintained from the sync snapshot and the
  `order` / `orderVersion` events too, so the REST poll drops to every 10 s while the
  socket is synced (2 s when it is down, orders every second poll, up to 30 s after a
  rate limit). The poll wakes immediately when the socket drops. The 429 body is kept in
  Diagnostics (`last_429`).

## 5.0.0-alpha.40
- **Copy trading: Tradovate's rate limit is a throttle, not a lost feed.** A 429 on the
  leader poll marked the feed lost and could flatten the followers. `TradovateSession`
  now raises `RateLimited` with the broker's penalty time (`p-time`); the copy poll waits
  it out, slows down a step (up to 5 s, back to 1 s after a clean minute) and keeps the
  feed marked up — the feed tag shows *live · throttled*. The leader's orders are read
  every second poll to halve the request volume.

## 5.0.0-alpha.39
- **Copy trading: the socket can no longer flatten your followers.** Tradovate's WebSocket
  answered `authorize` but never the sync request and dropped the connection every few
  minutes; each drop counted as a lost feed, so after the grace period the followers were
  flattened and the group paused although the REST backstop was healthy all along. The feed
  is now the 1-second REST poll of the leader's orders and positions — it alone decides
  *feed lost* and the flatten watchdog — and the WebSocket runs beside it purely as an
  accelerator: its events are applied the moment they arrive, and losing it is logged
  (`ws_lost` / `ws_up`) but never counts as losing the feed. The sync request is sent only
  after the authorize answer (Tradovate drops requests sent earlier) and a sync without
  answer reconnects the socket. The feed tag shows *socket + poll* or *poll (socket down)*.
  Fixed the `[object HTMLDivElement]` lines in the drawer's Live block.

## 5.0.0-alpha.38
- **Copy trading stage 2: working orders are mirrored** (`app/copy_orders.py`, group switch
  *Mirror working orders*, on for new groups — switch it on for existing ones). Every working
  limit / stop / stop-limit order of the leader gets a twin on each follower, sized by the
  group's rule, following the leader's modifications and cancelled when the leader's order is
  gone. Stop / target pairs become one OCO pair on the follower (`/order/placeoco`), so a
  filled follower stop cancels the follower target broker-side. On a leader fill the twins
  are cancelled first and the follower's real broker position decides the market order, so a
  filled twin is never doubled. Twins are persisted (`copy_twins`), verified after a restart
  and reconciled every 10 s (missing → re-created, orphans → cancelled, gone at the broker →
  dropped). Market / trailing / exotic orders, baseline contracts and filtered symbols are
  skipped with an `order_skip` event. Drawer shows the leader's working orders and each
  follower's twins. 8 tests.

## 5.0.0-alpha.37
- **Rollover with confirmation.** The daily rollover check now proposes the next contract
  per mapped symbol — from the broker's listing when a login is connected (first month after
  the current one, with its expiry date), otherwise estimated, and never a month that is
  itself already past — and Settings → Symbol Mapping shows a *Rollover due* card with the
  proposals (editable, tick / untick) and **Apply selected rollovers** behind a confirmation
  dialog. `GET /api/rollover`, `POST /api/rollover/apply`. The Overview banner links to the
  review; the alert text points there too. 2 tests.

## 5.0.0-alpha.36
- **Risk guard per trade account** (`app/risk.py`, Settings → Tradovate Accounts → Risk
  guard). Daily **loss limit**, daily **profit target** and a fixed **flatten time** per
  account. Evaluated on every live P&L poll against the broker's own today's P&L
  (realised + open). A hit flattens the account (cancel all working orders, close every
  position at market) and **locks** it for the rest of the local day: the lock is enforced
  in `TradovateSession.place_order`, the single path every order takes (webhooks, Discord,
  marketplace, copy trading), refused orders appear as rejected in the order log, and a
  position that reappears while locked is closed again. *Unlock for today* clears the lock
  by hand; it clears itself with the next local day. `locked` tag on the Overview, *Risk
  guard fired* alert (all channels), `GET /api/risk`, `POST /api/risk/unlock`. The P&L
  poll runs at the fast cadence whenever any account has a rule. 5 tests.

## 5.0.0-alpha.35
- **Copy trading: 1-second backstop, socket capture.** Live groups mirrored every change
  through the REST backstop (`ws_miss`), i.e. the socket delivered no position events; the
  backstop now runs every second so the worst-case delay matches the poll feed, `ws_miss`
  rows carry the contract name, and the Diagnostics block keeps the last 20 raw socket
  messages so the event shape Tradovate actually sends can be read off the drawer.

## 5.0.0-alpha.34
- **Copy trading: no silent paths, REST backstop, diagnostics.** A live group saw the
  leader's trade but sent no follower order and logged nothing. Every branch that could
  swallow a mirror now leaves a trace: the leader's Tradovate account id is resolved from
  the login's account list when settings lack it (an unknown id previously filtered *every*
  socket event out, silently) and the group reports it; a symbol outside the filter logs
  `filtered` once per contract; any exception while placing a follower order — not only
  Tradovate errors — logs `reject` and raises the alert; a failed `user/syncrequest` marks
  the feed lost with the reason. On the WebSocket feed the leader's positions are also read
  over REST every 5 s and any change the socket did not deliver is mirrored immediately and
  logged as `ws_miss`. Reconcile now compares each follower with its **target** (not only
  with the last mirror), so a follower that never received an order is filled within 10 s,
  with a 30 s hold-off after a reject. The drawer shows a **Diagnostics** block (leader
  account id, user id, frames, sync response, event counts per entity type, last position
  event, baseline contracts).
- **Symbols picked from the symbol mapping.** The group's symbol filter is a set of chips
  built from Settings → Symbol Mapping (map keys, mapped contracts and allowed roots — a
  dated `MNQU6` counts as `MNQ`), plus a free-text field for other roots and an *every
  contract* switch.

## 5.0.0-alpha.33
- **Copy Trading** (new Routing module, `app/copy.py`, Routing → Copy Trading). Mirror one
  leader trade account onto any number of follower accounts (own or third-party logins) in
  real time: entries, adds, reductions, closes and reversals — sized per follower by
  **multiplier** or **fixed** contracts (fixed follows the leader's adds proportionally,
  switchable), with a **symbol** filter, **direction** filter and **max** cap. The engine is
  a position mirror: on every leader change one market order for the difference, self-healing
  through a 10-second reconcile against the followers' broker positions. Leader feed over
  Tradovate's WebSocket **user sync** (~100 ms), polling once a second for logins that execute
  through an agent. A feed lost for longer than the configured seconds **flattens the
  followers** at market and pauses the group (resume / sync now / flatten by hand from the
  drawer). Existing leader positions are baseline (not copied until flat or synced), loops
  between groups are rejected, the global Trading switch applies. Event log with per-mirror
  latency (`copy_events`, 7 days), *Copy trading* alert trigger for rejects and pauses,
  `/api/copy/*` endpoints, 16 tests. New dependency: `websockets`.

## 5.0.0-alpha.32
- **Trailing drawdown done properly** (`app/drawdown.py`). Tradovate's risk record only
  carries the drawdown *size* and the *cap* at which trailing stops — not the threshold.
  The bridge now tracks the account's **peak** itself: for **Intraday** accounts the
  highest equity incl. open P&L, tick by tick; for **EOD** accounts the highest session
  close (17:00 New York), seeded from the journal's daily balances where history exists.
  Threshold = min(peak, cap) − size; room = equity − threshold. Peaks only ratchet up and
  are persisted per account. Because the bridge can only know peaks from the moment it
  watches an account, each row has a ✎ to **pin the threshold your prop firm shows** —
  from then on the figures are exact; an ≈ marks rows that are tracked but not pinned.
  Hover the cell for peak, size, cap and since-when.

## 5.0.0-alpha.31
- **Max trailing drawdown on the Today's P&L card.** Each account row shows the room left
  to Tradovate's trailing-drawdown liquidation level (equity = balance + open P&L, minus
  the level), the level itself and whether the account trails **EOD** or **Intraday**
  (Tradovate "RealTime"). Intraday is highlighted amber; the room turns amber below half
  the drawdown and red below a quarter or once it is used up. Read from the login's
  `userAccountAutoLiq` records (one call per login per poll); accounts without a record
  show a dash. The column is sortable like the others.

## 5.0.0-alpha.30
- **Passphrase is verified before the marketplace fan-out** (`app/signals.py`). The
  publisher's signal was forwarded to subscribers *in parallel* with the publisher's own
  execution, and subscribers run with `trusted=True` (they cannot know the passphrase) —
  so anyone who merely knew the webhook URL could trade on every subscriber's accounts
  while the publisher's own execution failed. `accept()` now checks first: a wrong or
  missing passphrase executes nothing anywhere, logs the rejection and fires the
  "signal not executed" alert. `process()` keeps its own check for direct callers.
- **`close_all` only cancels its own contract's orders** (`app/engine/{common,manage,ts_hunter}.py`).
  A close for one symbol cancelled *every* working order on the account, stripping the
  stops and targets of positions in other symbols and leaving them unprotected. Orders are
  now matched to the contract by Tradovate's numeric `contractId` (new `contract_id()`
  lookup, cached) or by contract name; orders of other contracts are kept and the count is
  logged. If orders carry no contract information at all the old account-wide behaviour
  stands, so the closed contract's stops can never re-fill. The SOS **Flatten all** stays
  account-wide by design.

## 5.0.0-alpha.29
- **Privacy mode** (eye icon in the top bar next to the theme toggle, also in the user
  menu): masks account names everywhere they are shown — first six characters, the rest
  as asterisks — for screenshots, screen sharing and streaming. Applies to the Today's
  P&L card, positions, tracked trades, orders, logs, the journal (table, filter, detail),
  the accounts / alerts / webhook / marketplace lists. Remembered per browser; the page
  repaints immediately when toggled.

## 5.0.0-alpha.28
- **Sortable, live Today's P&L card.** Column headers (Account, Realised, Open, Week,
  Balance) sort the per-account rows, click again to flip; the default order puts the
  accounts that traded today on top (largest movement first) and idle ones last. A
  **Hide idle** toggle removes accounts with no realised, open or weekly P&L; the sub-line
  counts active vs idle. Values that changed since the last poll flash briefly, idle rows
  are dimmed. Sort and toggle are remembered per browser.

## 5.0.0-alpha.27
- **Fix duplicate journal trades.** The Performance-report import stored round trips under
  the same key family (``pair:``) as the live fill-pair import, and the cross-source dedup
  deliberately skips same-family keys — so a trade that arrived from both showed up twice.
  Report trades are now keyed ``rpt:``, CSV uploads ``csv:``; every import path checks for
  the same round trip under another source first (matching broker fill ids, or the same
  account / symbol / side / size / prices with the exit within 5 s). Existing duplicates are
  collapsed once at startup (the row from the most authoritative source survives, notes and
  tags are carried over); **Journal → Remove duplicates** runs the same cleanup on demand.

## 5.0.0-alpha.26
- **Fix likely iPhone push cause: VAPID contact was ``mailto:admin@localhost``.** Apple
  validates the ``sub`` contact and rejects an invalid host with 403 BadJwtToken. The
  contact now uses ``NEXUSPRED_PUBLIC_URL`` or the real host the dashboard is opened on
  (learned when a device subscribes), never ``localhost``.
- Diagnostics box is readable in dark mode (was white-on-white).

## 5.0.0-alpha.25
- Fix: the Alerts page failed to render in alpha.24 (Diagnose button referenced before it was declared).

## 5.0.0-alpha.24
- **Push diagnostics button** (Settings → Alerts → Diagnose, admin). Shows the server's VAPID key fingerprint, crypto source and whether the stored key decrypts under the current key, then does a live, non-pruning send to every device and prints the raw push-service answer per device — so an Apple 403/410 is visible verbatim instead of only a counter.

## 5.0.0-alpha.23
- **Serve JavaScript with ``no-store``.** ES modules import their siblings with un-versioned
  relative paths, and a standalone iOS PWA served those from cache without revalidating even
  under ``no-cache`` — so a device could keep running an old bundle (e.g. the pre-fix push
  client) after a deploy. JS is now ``no-store`` (always fetched fresh); CSS stays ``no-cache``
  since it is already version-busted.

## 5.0.0-alpha.22
- **Fix the iPhone re-enable loop.** iOS Safari never exposes a subscription's
  ``applicationServerKey``, so the previous "same key?" check could not tell a stale
  subscription (bound to a rotated VAPID key) from a good one and reused it — enabling
  then failed and the server pruned the device, over and over. Enabling now recreates the
  browser subscription whenever the key cannot be positively confirmed, so on iOS it
  always subscribes under today's key. Enable once more per device to recover.

## 5.0.0-alpha.21
- **Fix iPhone push (403 BadJwtToken).** The VAPID keypair is stored encrypted in the
  meta table but was not covered by the startup re-encryption pass, so a change to the
  encryption key left it unreadable and the bridge silently generated a *new* keypair —
  invalidating every existing subscription (Apple then rejects with 403 BadJwtToken while
  a device that subscribed under the new key still works). Now: the key is re-encrypted
  under the current crypto key instead of being regenerated; enabling push on a device
  whose stored server key no longer matches drops the stale subscription and subscribes
  afresh; and a 403 BadJwtToken / key-hash mismatch prunes the subscription (like 410) and
  logs a re-enable hint. Re-enable push once per affected device to fix it.

## 5.0.0-alpha.20
- **Push diagnostics.** A failed push now records the push service's actual HTTP status
  and body (Apple / FCM), shows it under the device on Settings → Alerts, and logs it to
  the event log — so an iPhone that "fails" says *why* (e.g. 403 BadJwtToken, 410 gone)
  instead of a bare counter. The VAPID token expiry is pinned to 12 h explicitly (Apple
  rejects anything over 24 h with any clock skew).

## 5.0.0-alpha.19
- **Alert accounts** (Settings → Alerts → Accounts, `alert_accounts`): choose which trade
  accounts may raise account-level alerts — position opened / added / closed / reduced,
  signal executed and the daily summary. Empty (the default *All accounts*) keeps the old
  behaviour; with a selection, other accounts are still tracked but stay silent, and the
  daily summary totals only the selected ones. Connection alerts are per login and unaffected.

## 5.0.0-alpha.18
- **Position alerts from the broker's view** (`app/watch.py`). The live P&L poll now also
  reads each login's position list and diffs it tick to tick: *Position opened* (account,
  symbol, direction, size, price), *added*, *reduced* and *closed* — the last two with the
  account's **realised P&L change** for that close and the time the position was open. Fires
  for stop / target fills and manual trades too, not just bridge signals; positions already
  open at startup are the baseline. Discord + push.
- **Execution agent offline / online alerts** (Discord + email + push) and a **daily summary**
  at a configurable local time (`daily_summary_time`, default 22:05 in the journal timezone):
  realised P&L per account, trades closed, wins / losses — on every channel.
- New switches on Settings → Alerts: position opened, position closed, agent offline, agent
  online, daily summary (+ time). The P&L poll runs at the fast cadence whenever position
  alerts are on, not only while a dashboard is open.

## 5.0.0-alpha.17
- **Security review round 2** (findings from a fresh assessment incl. the execution
  agents and push channel; all fixed):
  - *Rate-limit bypass via `X-Forwarded-For`* (confirmed against the live host): the
    limiter keyed on the first, client-written hop. The client address is now the hop
    the trusted proxy appended (`NEXUSPRED_PROXY_HOPS`, default 1), and two
    address-independent brakes were added — 20 failed logins per account / 10 min and
    300 failures per minute server-wide (`login_blocked` audit rows).
  - *Cross-workspace agent hijack*: a user could set another tenant's `agent_id` on their
    logins and trade (with their Tradovate tokens) through that tenant's VPS. Agent ids
    are now validated against the caller's workspace on save and again in the relay.
  - *Agent as an open proxy*: the agent executed any URL the bridge handed it. Agent
    **v1.2.0** only calls `https://*.tradovateapi.com` / `*.tradovate.com` (redirects
    elsewhere refused) and the bridge enforces the same list; the bridge URL must be
    `https://`.
  - *Webhook passphrase in signal logs*: the raw payload (incl. `passphrase`) was
    persisted, streamed and forwarded to marketplace subscribers. Credential-like payload
    fields are masked everywhere; forwarded copies drop the passphrase.
  - *Push endpoint SSRF*: `/api/push/subscribe` accepted any `https://` URL the bridge
    would later POST to. Endpoints pass the SSRF guard, keys are length-checked, at most
    25 devices per workspace, and subscribe/test are rate-limited.
  - *SMTP host* is validated like other outbound targets (no internal addresses, port
    1–65535) and `starttls` now verifies the server certificate.
  - *Memory growth from unknown agent tokens*: failed token lookups were cached forever;
    misses are no longer cached and the cache is bounded.
  - *Agent .exe supply chain*: the release now carries `fluxbridge-agent.exe.sha256` and a
    GitHub build-provenance attestation; the bridge refuses to bundle an .exe whose hash
    does not match.
  - Deleting a user now revokes their agents, pairing codes and push devices.
- Dependencies: floors raised to what CI already runs (cryptography 50.0.1, packaging 26.3,
  discord.py-self 2.1.0, pytest 9.1.1, pytest-asyncio 1.4.0) and the GitHub Actions bumped to
  their current majors (checkout 7, setup-python 7, setup-node 7, upload-artifact 7) —
  consolidates the nine open Dependabot PRs into one change.

## 5.0.0-alpha.16
- **Push notifications** (`app/push.py`, `static/js/sw.js`, `static/js/push.js`). A third
  alert channel next to Discord and email: Web Push to every device that enabled it on
  **Settings → Alerts → Push notifications** — desktop browsers and the iPhone/iPad
  Home Screen app (iOS 16.4+). One master switch (`alert_push_enabled`) plus the existing
  per-trigger switches; push devices get every trigger, trade executions included, and a
  tap opens the relevant page. The bridge generates its own VAPID key pair on first use
  and stores it encrypted; payloads are end-to-end encrypted by `pywebpush` (new
  dependency). Registered devices are listed with a per-device test push and remove
  button; subscriptions that the push service reports as gone (404/410) are pruned
  automatically. The service worker is served from `/sw.js` (public, `no-cache`) and
  deliberately never caches pages, so deploys still load fresh. New DB table
  `push_subscriptions`; new endpoints `/api/push/*`; `[hidden]` now beats `.btn` in CSS.

## 5.0.0-alpha.15
- **Preconfigured agent download.** Settings → Execution Agents → *Download
  preconfigured agent* registers the agent and hands you a zip that already contains
  `agent.json` (bridge URL + the agent's token) and, when the release build is
  reachable, **`fluxbridge-agent.exe`** — unzip on the VPS, start it, nothing to type,
  no Python needed. A new workflow (`agent-exe.yml`) builds the .exe with PyInstaller on
  every change under `agent/` and publishes it to the rolling GitHub release
  `agent-latest`, which the bridge bundles (cached for an hour). The plain agent and the
  pairing-code flow remain available. `start-agent.bat` prefers the .exe when present;
  the agent reads `agent.json` next to the executable when frozen.

## 5.0.0-alpha.14
- **Execution agents — one IP per account** (`app/relay.py`, `agent/`). A small
  stdlib-only Python helper runs on a VPS, pairs with the bridge through a **one-time
  pairing code** (Settings → Execution Agents; 15 min, single use) and receives a token
  that is valid for the relay endpoints only — the dashboard login never leaves the
  bridge. Under Settings → Tradovate Accounts each login gets **Execute via**: bridge
  (direct) or a paired agent. Everything that login does with Tradovate — orders,
  modifications, cancels, token renewal, health checks, positions, P&L snapshots — is
  then executed by the agent from its own IP; the agent long-polls the bridge
  (outbound HTTPS only, no open port). If the agent is offline the login's calls fail
  with a clear error and the usual alerts; the bridge never falls back to its own IP.
  Agents show online/offline, last IP and version; revoking kills the token at once.
  Download the agent from the settings page; `agent/README.md` has the VPS setup.
  Saving token logins now also keeps the discovered trade accounts (previously they
  were dropped until the next Connect & Verify).

## 5.0.0-alpha.13
- **Encryption key changes no longer lose secrets.** A secret that does not decrypt with
  the current key is tried against the previous keys (`NEXUSPRED_ENCRYPTION_KEY_PREVIOUS`,
  the environment's `SESSION_SECRET`, the database's auto-generated secret) and
  re-encrypted with the current key at the next start. Introducing
  `NEXUSPRED_ENCRYPTION_KEY` on an existing deployment therefore keeps the stored
  Tradovate / Discord tokens intact.

## 5.0.0-alpha.12
- **Live P&L on the Overview** (`app/pnl.py`). A new *Today's P&L* card at the top of the
  start page shows, per connected trade account and in total, today's realised P&L, the
  open (unrealised) P&L of current positions, the week's realised P&L and the cash
  balance — the broker's own figures from Tradovate's cash-balance snapshot, so no
  market-data feed is needed. Polled every `pnl_poll_seconds` (default 5, Settings →
  General) while a dashboard is open, once a minute when idle, backing off on errors;
  changes are pushed over the live stream (`kind: "pnl"`), so the number moves within a
  few seconds of a fill or a tick. `GET /api/pnl` (add `?refresh=1` to poll now);
  `/api/status` carries the last snapshot for the first paint.

## 5.0.0-alpha.11
- **Report windows adapt to the service.** Tradovate's reporting service answers
  "Too long range" above a certain span; the history import now starts with 30-day
  windows, halves on that answer until accepted, remembers the accepted size
  (`journal_report_window`) and paces requests. Up to 150 windows per login and run;
  remaining days are reported (`pending_days` in the diagnostics) and fetched next run.

## 5.0.0-alpha.10
- **History via Tradovate's reporting service.** The first real import showed that the
  entity lists *and* the cash-balance log only cover the current session. The web
  platform's Reports tab uses a separate reporting service (`rpt-live` / `rpt-demo`
  hosts, `POST /v1/reports/requestreport`) that accepts any date range; the journal now
  requests the **Performance** report (one row per round trip with the broker's P&L)
  per account in 90-day windows — first run as far back as *History to import (days)*
  (default 365), later runs only from the last covered day minus 3 days — parses it like
  a CSV export and stores the trades keyed by fill ids. New settings: history depth and
  a flat *fee per contract per side* for report/CSV trades. Diagnostics list the report
  definitions the account offers and, per account, windows / rows / columns / errors.

## 5.0.0-alpha.9
- **Import diagnostics.** Every journal import now records what each Tradovate endpoint
  returned (row counts, column names, cash-log change types, date span — never prices,
  ids or balances); click a row in the Imports table to see it and copy it. Accounts
  saved without ids (before *Connect & Verify*) are resolved via `/account/list`; the
  cash-balance log is also tried per account (`/cashBalanceLog/deps`) and accepts
  `tradeId` on FillPair entries as the pair reference.

## 5.0.0-alpha.8
- **Journal history straight from Tradovate — no export needed.** Every import (daily
  and *Import now*) now walks the account's **cash-balance log**
  (`/cashBalanceLog/list`), the broker's own book that reaches back over the account's
  life: each realised fill pair not yet journaled is fetched by id (`/fillPair/items` →
  `/fill/items` → contract/product) and stored as a trade with the **broker's realised
  P&L** and the per-fill fees from the same log; the log's running balance becomes one
  equity snapshot per account and trading day. Processed pair ids are remembered
  (`journal_seen`), so later runs are incremental (max 2 000 new pairs per run). Fill
  pairs whose fills are no longer in the session list are also resolved by id. The
  Imports table shows a **From history** column; CSV import stays as a fallback.

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
