/* TradingView alert-message templates, test-signal presets, Discord test embeds,
   and the token bookmarklets — shared by the Webhooks drawer and the Tools page. */

/**
 * TradingView alert-message JSON for a strategy, using TradingView's own
 * placeholders so it can be pasted straight into the alert's Message box.
 * Numeric placeholders are unquoted so the substituted value stays a number.
 */
export function alertMessageTemplate(strategy) {
  if (strategy === "ts_hunter") {
    return {
      json: JSON.stringify({
        contract_version: "at_execution_command_v5", event: "signal",
        side: "BUY", symbol: "MNQ",
        risk: { mode: "fixed_lot", value: 4 },
        sl: { mode: "fixed_price_from_alert", value: 0 },
        trade_id: "unique-id-per-trade",
      }, null, 2),
      hint: "TS-Hunter expects the exact JSON your TS-Hunter Pine strategy already sends "
        + "(entry as event:\"signal\", then event:\"management\" messages with "
        + "action:\"partial_close_percent\" for TP1/TP2/TP3 and action:\"full_close\" to "
        + "flatten) — all correlated by trade_id. Point that strategy's alert(s) at this "
        + "webhook's URL; there's nothing to hand-edit here. Each partial close resizes "
        + "the stop to the new remaining qty (price unchanged).",
    };
  }
  if (strategy === "bracket") {
    return {
      json: JSON.stringify({
        action: "{{strategy.order.action}}", symbol: "{{ticker}}",
        entry: "{{strategy.order.price}}", sl: 0, tp1: 0, tp2: 0, tp3: 0,
      }, null, 2).replace('"{{strategy.order.price}}"', "{{strategy.order.price}}"),
      hint: "action/symbol/entry are filled in automatically by TradingView. There's no "
        + "built-in placeholder for sl/tp1/tp2/tp3 — replace those 0s with your own "
        + "strategy's stop/target levels (e.g. {{plot(\"SL\")}} if you plot them in Pine), "
        + "or drop any tp you don't use.",
    };
  }
  return {
    json: JSON.stringify({
      action: "{{strategy.order.action}}", symbol: "{{ticker}}", qty: "{{strategy.order.contracts}}",
    }, null, 2).replace('"{{strategy.order.contracts}}"', "{{strategy.order.contracts}}"),
    hint: "action, symbol and qty are filled in automatically by TradingView from the strategy order — nothing to edit.",
  };
}

export const STRATEGY_LABEL = { simple: "simple", bracket: "bracket", ts_hunter: "TS-Hunter" };
export const STRATEGY_OPTIONS = [
  { value: "simple", label: "simple — buy/sell only" },
  { value: "bracket", label: "bracket — entry + TP/SL" },
  { value: "ts_hunter", label: "TS-Hunter — signal + partial closes" },
];

export const PRESETS = {
  simple_buy: { label: "Simple buy", payload: { action: "buy", symbol: "MNQ1!", qty: 2 } },
  simple_sell: { label: "Simple sell", payload: { action: "sell", symbol: "MNQ1!", qty: 2 } },
  entry: { label: "Bracket entry (sell)", payload: {
    event: "entry", action: "sell", symbol: "MNQ1!", entry: 30267,
    sl: 30285.06839, tp1: 30261.57948, tp2: 30265.19316, tp3: 30247.0425,
    qty: 4.95623, risk_usd: 179.10204,
  } },
  move_sl: { label: "Move SL", payload: {
    event: "tp1_hit", action: "move_sl", symbol: "MNQ1!", new_sl: 30266.01,
    message: "TP1 reached — SL moved to net-breakeven",
  } },
  trail: { label: "Trail active", payload: {
    event: "tp2_hit", action: "trail_active", symbol: "MNQ1!",
    trail_ema: "ema9", trail_buffer: 0.15, message: "TP2 reached — trailing stop active",
  } },
  close: { label: "Close all", payload: {
    event: "tp3_hit", action: "close_all", symbol: "MNQ1!",
    exit_price: 30241.70425, pnl: 250.70285, message: "TP3 full kill — close all",
  } },
  runner: { label: "Runner exit", payload: {
    event: "runner_exit", action: "close_all", symbol: "MNQ1!",
    exit_price: 29761.94756, realized_R: 1.7, message: "Runner trailed out past TP3 — closed in profit",
  } },
  ts_signal: { label: "TS-Hunter signal", payload: {
    contract_version: "at_execution_command_v5", event: "signal", side: "SELL", symbol: "MNQ",
    risk: { mode: "fixed_lot", value: 4 }, sl: { mode: "fixed_price_from_alert", value: 29658.5 },
    tv: { entry_price: 29329 }, trade_id: "TS-HUNTER-DEMO-1",
  } },
  ts_tp1: { label: "TS-Hunter TP1 (25%)", payload: {
    contract_version: "at_execution_command_v5", event: "management", action: "partial_close_percent",
    percent: 25, lifecycle_stage: "TP1", symbol: "MNQ", trade_id: "TS-HUNTER-DEMO-1",
  } },
  ts_close: { label: "TS-Hunter full close", payload: {
    contract_version: "at_execution_command_v5", event: "management", action: "full_close",
    reason: "sl_hit", symbol: "MNQ", trade_id: "TS-HUNTER-DEMO-1",
  } },
};

export const DS_TEST_PRESETS = {
  entry: { label: "Entry (SELL)", embed: {
    title: "AkSniper 🎯 · SELL MNQ",
    fields: [{ name: "Contracts", value: "3" }, { name: "Entry", value: "20450.25" }, { name: "Time", value: "10:31" }],
  } },
  update: { label: "Stop / target moved", embed: {
    title: "AkSniper 🎯 · Stop / target moved · MNQ",
    fields: [{ name: "Stop", value: "20440.0 → 20450.0" }, { name: "Target", value: "20500.0 → 20520.5" }, { name: "Position", value: "3" }],
  } },
  close: { label: "Closed", embed: {
    title: "Closed MNQ · +90.75 pts",
    fields: [{ name: "P&L", value: "+$181.50" }, { name: "Move", value: "+90.75" }, { name: "Exit", value: "20541.0" }, { name: "Held", value: "12m" }],
  } },
  junk: { label: "Unrecognised", embed: {
    title: "Brand-new message type nobody expected",
    fields: [{ name: "Whatever", value: "???" }],
  } },
};

// No-install alternative to the extension: drag to the bookmarks bar, click on
// the site. Discord deletes window.localStorage in the page, so we read it from
// a fresh same-origin iframe. Tradovate keeps tokens in normal storage.
export const BOOKMARKLETS = {
  discord:
    "javascript:%28function%28%29%7Btry%7Bvar%20f%3Ddocument.createElement%28%27iframe%27%29%3Bdocument.body.appendChild%28f%29%3Bvar%20r%3Df.contentWindow.localStorage.getItem%28%27token%27%29%3Bf.remove%28%29%3Bvar%20v%3Dr%3Fr.replace%28%2F%5E%22%2B%7C%22%2B%24%2Fg%2C%27%27%29%3Anull%3Bif%28v%29%7Bif%28navigator.clipboard%29navigator.clipboard.writeText%28v%29%3Bwindow.prompt%28%27Discord%20user%20token%20%28copied%29%3A%27%2Cv%29%3B%7Delse%7Balert%28%27No%20Discord%20token%20found.%20Log%20in%20on%20discord.com%20and%20try%20again.%27%29%3B%7D%7Dcatch%28e%29%7Balert%28%27Could%20not%20read%20token%3A%20%27%2Be%29%3B%7D%7D%29%28%29%3B",
  tradovate:
    "javascript:%28function%28%29%7Bfunction%20g%28k%29%7Btry%7Breturn%20localStorage.getItem%28k%29%7C%7CsessionStorage.getItem%28k%29%7Dcatch%28e%29%7Breturn%20null%7D%7Dfunction%20u%28x%29%7Breturn%20x%3Fx.replace%28%2F%5E%22%2B%7C%22%2B%24%2Fg%2C%27%27%29%3Ax%7Dvar%20t%3Du%28g%28%27token%27%29%29%2Cc%3Du%28g%28%27checkToken%27%29%29%3Bif%28navigator.clipboard%26%26t%29navigator.clipboard.writeText%28t%29%3Bwindow.prompt%28%27Tradovate%20token%20%28copied%29%3A%27%2Ct%7C%7C%27%28not%20found%29%27%29%3Bwindow.prompt%28%27Tradovate%20checkToken%3A%27%2Cc%7C%7C%27%28not%20found%29%27%29%3B%7D%29%28%29%3B",
};

// The canonical origin webhook URLs are shown on: NEXUSPRED_PUBLIC_URL from
// /api/status when the server pins one (custom domain), else the page's own origin.
let publicOrigin = "";
export function setPublicOrigin(origin) {
  publicOrigin = String(origin || "").replace(/\/+$/, "");
}

export function webhookUrl(token) {
  return `${publicOrigin || window.location.origin}/webhook/${token || "your-token"}`;
}
