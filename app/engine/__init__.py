"""Strategy handlers behind :mod:`app.signals`.

* :mod:`.common`    — shared primitives (errors, symbol resolution, cancels)
* :mod:`.simple`    — plain buy/sell execution
* :mod:`.bracket`   — entry + TP1-3 + SL bracket, stop moves / resizes
* :mod:`.manage`    — strategy-agnostic position management (close_all, set_sl_tp)
* :mod:`.ts_hunter` — the TS-Hunter contract (trade_id-correlated lifecycle)

The entry point, per-trade locking and active-trade tracking live in
``app.signals``; the handlers here are pure "execute this on these accounts".
"""
