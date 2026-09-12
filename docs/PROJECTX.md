# ProjectX (TopstepX, Bulenox, Alpha Futures, …) — adapter notes

`app/projectx.py` implements the bridge's broker interface over the **ProjectX Gateway
REST API**. Status: built against the public gateway documentation and a mocked gateway
(`tests/test_projectx.py`); **not yet verified with a real API key**. The live gateway
answered the login call during the build (wrong key → error code), so the endpoint layout
is right; field-level details are to be confirmed on a practice account.

## Setting up a login

Settings → Tradovate Accounts → *Add login*, Broker **ProjectX (Topstep …)**:

| Field | Value |
|---|---|
| User name | your ProjectX / TopstepX user name |
| API key | from the firm's dashboard (TopstepX: Settings → API) |
| Firm | `topstep`, `alphaticks`, `bulenox`, `blusky`, `e8x`, `tradeify`, … (the list in the field), `demo` for the ProjectX demo gateway, or a full `https://` gateway URL |
| Env | *Demo* for practice / evaluation accounts, *Live* for funded accounts (selects live vs. sim contract data) |

Save, then **Connect & Verify**: the login exchanges the key for a token (renewed every 20
hours), lists the active accounts and stores them. Route webhooks to the accounts as usual.

## What is mapped

| Bridge | ProjectX |
|---|---|
| accounts | `Account/search` (`onlyActiveAccounts`) — ids are the gateway's integers |
| positions | `Position/searchOpen` per account; long/short → `netPos` ± size |
| working orders + versions | `Order/searchOpen` / `Order/search` (last 36 h); status 1/6 = working, 2 filled, 3 cancelled, 4 expired, 5 rejected |
| place | `Order/place` — Market 2, Limit 1, Stop 4, StopLimit 3; side 0 buy / 1 sell |
| modify / cancel | `Order/modify`, `Order/cancel` (account id from the executor) |
| flatten | `Position/closeContract` |
| OCO pair | second order with `linkedOrderId` = first (verify that a fill cancels the sibling) |
| cash snapshot | balance from the account list; realised = today's `Trade/search` P&L minus fees (CME day, 17:00 New York); open P&L estimated from the last 1-minute bar × tick value |
| contracts | `Contract/search` / `searchById`; front month = the `activeContract`; two-digit years (`MNQZ25`) shown in the bridge's form (`MNQZ5`); root aliases `NQ→ENQ`, `ES→EP`, `CL→CLE`, `GC→GCE`, … |
| rate limit | one request per 0.3 s per login; a 429 becomes `RateLimited` with the retry-after (engines back off like they do for Tradovate) |

Not covered: SignalR real-time hubs (the bridge polls), execution-agent routing, journal
import, RMS / drawdown rules (`auto_liq_rules` is empty — use the bridge's own risk guard).

## First real test (practice account)

1. Connect & Verify; check the account list and the *firm* in the login status.
2. Send a market entry + stop + limit target from a webhook; watch **Logs → Orders**: the
   `orderId`, the status mapping and the prices.
3. Modify the stop, cancel the target, close all — each must appear in the gateway's order
   history with the expected state.
4. Copy trading with a ProjectX practice account as follower, then as leader (poll feed).
5. Compare the Overview's realised / open P&L with the firm's dashboard for one session.
   If open P&L is off, the tick value or the bar endpoint's fields differ — adjust
   `cash_snapshot` / `_last_price`.
