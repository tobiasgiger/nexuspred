# Rithmic as a second broker — analysis (not implemented)

What it would take to run Fluxbridge accounts through Rithmic (R|Trader Pro, Apex/Topstep/
MFFU "Rithmic" accounts) next to Tradovate. Written 2026-09 against v5.0.0-alpha.56.

## 1. How Rithmic differs from Tradovate

| | Tradovate | Rithmic |
|---|---|---|
| Transport | HTTPS REST + JSON, one WebSocket for user sync | Persistent TLS **WebSocket** (`wss://…rithmic.com:443`), **Protocol Buffers** frames only, no REST |
| Auth | Access token (renewable) per login | Username + password + `system_name` (e.g. `Rithmic Paper Trading`, `Apex`, `TopstepTrader`) and an **app name / app version registered with Rithmic**; no tokens |
| Order entry | `POST /order/placeorder` → order id; OCO via `placeoco` | `RequestNewOrder` (template 312) → `ResponseNewOrder` + async `RithmicOrderNotification` / `ExchangeOrderNotification`; brackets via `RequestBracketOrder` (330); OCO via `RequestOCOOrder` (338) |
| Positions / P&L | `GET /position/list`, `cashBalanceSnapshot` | `RequestPnLPositionUpdates` (400) streaming; `RequestAccountRmsInfo` (304) for limits |
| Working orders | `GET /order/list` + `orderVersion` | `RequestShowOrders` (320) + streaming notifications |
| Contract lookup | `/contract/find`, `/contract/suggest` | `RequestSearchSymbols` (109) / `RequestFrontMonthContract` (113) on the **ticker plant** (a second connection) |
| Rate limit | 429 with `p-time` | None comparable; instead heartbeats every ~30 s or the socket is dropped |
| Conformance | Public API, self-serve token | **Rithmic conformance**: the app must be registered and pass a conformance test with Rithmic before it may connect to live systems; test systems (Rithmic Paper Trading) are open |

The last row is the one that decides the timeline: without Rithmic's sign-off the adapter
only ever works against the paper system.

## 2. What the codebase already isolates

* Every strategy, the copy engine, the risk guard, P&L and the journal talk to the broker
  through **`TradovateSession` / `AccountExecutor`** (`app/tradovate.py`), never through
  `httpx` directly. The executor surface is small and stable:
  `resolve_contract`, `contract_id`, `place_order`, `place_oco`, `modify_order`,
  `cancel_order`, `working_orders`, `liquidate_position`, `positions`, `order_versions`.
* `SessionManager.executor_for(...)` / `session_for(lid)` hand executors out by stable login
  id — callers do not care what is behind them.
* Per-account risk locks, sizing, alerts, order logging (`state.log_order`) and the copy
  twins all sit *above* the executor.

What is **not** isolated:

* `app/copy.py` and `app/copy_orders.py` call `session._request("GET", "/position/list")`,
  `/order/list`, `/contract/item` and open Tradovate's user-sync WebSocket themselves
  (`_run_ws`, `_on_ws_message`) — 11 call sites in total.
* `app/pnl.py`, `app/watch.py`, `app/journal.py` call `_request` for `cashBalanceSnapshot`,
  positions and the fills / performance reports.
* `app/rollover.py` uses `/contract/find` + `/contract/item` for expiry dates.
* `app/health.py` renews tokens (`_renew`) — meaningless for Rithmic (no tokens, but a socket
  that must be kept alive).
* Config: a login is `token_accounts[]` with `access_token`, `environment` demo|live and an
  `agent_id`; the execution agent relays **HTTP** requests only (`relay.allowed_url`).

## 3. Design that fits

1. **Broker interface.** Extract the executor surface into a `BrokerSession` protocol with
   `kind = "tradovate" | "rithmic"`, plus the four feed methods the engines need outside of
   orders: `positions_snapshot()`, `working_orders_snapshot()`, `contract_info(symbol)`,
   `fills_since(ts)`, `account_snapshot()` (cash, realised, open P&L). Move the copy engine's
   and P&L's raw `_request` calls behind those methods (this is a refactor worth doing even
   without Rithmic; it also makes the copy engine testable with one fake instead of a
   scripted `Sess`).
2. **Rithmic session** (`app/brokers/rithmic.py`): one asyncio task per login holding the
   order-plant socket (login, heartbeats, reconnect with resubscribe), a shared ticker-plant
   socket per system for symbol lookups, an outbound queue with request ids and futures, and
   a push handler that turns order / position / P&L notifications into the same in-memory
   picture the Tradovate poll builds today. Orders map 1:1 (market, limit, stop, stop-limit,
   OCO, bracket); `liquidate` = flatten via `RequestExitPosition` (3504).
3. **Protobufs.** Rithmic ships `.proto` files under NDA with the API package; they compile
   with `protoc`/`grpcio-tools` into a `rithmic_pb2` package (about 100 messages). Vendor the
   generated code, not the `.proto` files, and pin the API version.
4. **Copy-trading feed.** For a Rithmic leader the position stream *is* the feed (push, no
   polling, no 429s) — better than Tradovate. The feed-loss watchdog keys on the socket
   heartbeat instead of the poll timestamp.
5. **Execution agent.** The agent relays HTTP; a Rithmic login behind an agent needs the
   agent to hold the Rithmic socket itself and forward frames. That is a second agent mode
   (`agent-rithmic`), roughly the size of the current agent again. Without it, Rithmic logins
   trade from the bridge's IP only.
6. **Config / UI.** A login gets `broker: "rithmic"`, `system_name`, `username`,
   `password` (encrypted like tokens), `gateway` (Chicago / Europe / …); the Accounts page
   needs a broker selector and hides token fields for Rithmic; discovery comes from
   `RequestAccountList` (302). The journal importer needs a Rithmic branch (fills from
   `RequestShowOrderHistory` 322 or the PnL snapshot; Rithmic has no "performance report").
7. **Rollover.** Rithmic's `RequestFrontMonthContract` answers the exact question the
   estimator guesses today.

## 4. Effort and risks

| Piece | Size | Notes |
|---|---|---|
| Broker interface refactor (Tradovate behind it, copy/P&L/journal on it) | 3–4 days | Pure refactor, fully testable with the existing suite |
| Rithmic session: sockets, login, heartbeat, reconnect, request/response, notifications | 5–7 days | Needs a paper-trading account for development |
| Orders, OCO/bracket, modify, cancel, flatten, working-order and position pictures | 3–4 days | Semantics differ (e.g. bracket legs are separate orders with their own ids) |
| Accounts UI, config, discovery, encryption, health | 2 days | |
| Journal import + P&L snapshot | 2–3 days | No performance report; P&L must be derived |
| Agent mode for Rithmic | 4–5 days | Optional; only if prop firms require per-account IPs on Rithmic too |
| Rithmic conformance (registration, test, sign-off) | 2–6 weeks calendar | Out of our hands; nothing ships to live systems before it |

Total engineering: about four to five weeks of work, plus the conformance wait. The
prerequisite that costs nothing but time is Rithmic's developer registration — start it
first if the direction is wanted.

## 5. Status

**Adapter built (alpha.58), untested against a real Rithmic system.** `app/rithmic.py`
implements `BrokerSession` on top of the `async_rithmic` library (Protocol Buffers, plants,
reconnects); `token_accounts[i].broker = "rithmic"` with user, password, system name and
gateway; the Accounts page has a broker selector. Positions, working orders with versions,
cash snapshot, RMS rules, front-month lookup, market / limit / stop / stop-limit orders,
modify, cancel and flatten are mapped; the copy engine, risk guard, sizing, P&L and
alerts run unchanged on a Rithmic login (verified with a scripted client in
`tests/test_rithmic.py`).

What to do with the first real credentials (paper system first):
1. Settings → Tradovate Accounts → add a login, Broker *Rithmic*, Env *Demo*, user +
   password, system `Rithmic Paper Trading` (or the name your prop firm gives), gateway
   `paper`; Save, Connect & Verify. The accounts appear; the status shows the system.
2. Route a webhook to one account and send a test signal from the Simulator's payload
   through the real webhook on the paper system: market entry, stop, limit target, close.
3. Watch **Logs → Orders** for the `basket_id` and the mapped status texts. If Rithmic's
   status strings differ from the mapping in `RithmicSession._status`, adjust that one
   function (the test file has the edge cases).
4. Copy trading: a paper Rithmic account as follower first, then as leader (poll feed).

Known limits of this version: no broker-side OCO (two independent orders, warned once per
login), no execution-agent routing, no journal import for Rithmic logins, and live
systems need Rithmic's app registration (`NEXUSPRED_RITHMIC_APP_NAME` must be the
registered name) and conformance sign-off.

## 5a. Original status (alpha.57)

**Step 1 is done (alpha.57):** `app/broker.py` holds the protocols, the Tradovate session
implements them, and no module outside `app/tradovate.py` calls a Tradovate endpoint
directly any more. Tradovate behaviour is unchanged. Next: Rithmic API registration, then
the adapter against the paper system.

## 6. Recommendation

Do step 1 (the broker interface) as its own release regardless: it removes the last raw
Tradovate calls from the copy engine and P&L and shrinks the test fakes. Apply for Rithmic's
API access and the paper system in parallel. Decide on the full adapter once the
conformance path is clear and a first prop-firm account on Rithmic is at hand to test with.
