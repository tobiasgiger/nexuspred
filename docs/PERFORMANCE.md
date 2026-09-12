# Performance — how the bridge stays fast, and how to keep it that way

Measured on the signal hot path (webhook payload → engine → order call, fake broker,
bracket strategy, entry + close), one core, `bench_signal.py`:

| | alpha.60 | alpha.61 |
|---|---|---|
| Signals per second | ≈ 885 | ≈ 1 200 |
| Time per signal (whole pipeline incl. background history writes) | 1.13 ms | 0.83 ms |
| `load_settings()` calls per signal | 3.5 | **1.0** |
| One settings read (4.5 KB workspace) | 135 µs (`copy.deepcopy`) | 25 µs (pickled snapshot) |
| One settings read (22 KB workspace, many webhooks / groups) | 583 µs | 70 µs |
| CPU inside `signals.process` itself | — | ≈ 0.18 ms |

The broker round trip dominates in production (Tradovate ≈ 60–150 ms per order call from
US-East); everything the bridge does around it is a small fraction of that. What alpha.61
removed were the *hidden multipliers*: settings copied several times per signal and per
order, one broker request per account instead of per login, every workspace polled at the
busiest workspace's cadence, and a health loop that renewed every token as often as the
worst one.

## Architecture decisions that matter

| Decision | Why |
|---|---|
| One uvicorn worker, asyncio everywhere | Order flow is I/O-bound; one event loop avoids cross-process locks and keeps the in-memory state (settings cache, session objects, copy runners) coherent. Scale vertically; a second worker would double broker sessions. |
| Settings cached per workspace in memory, handed out as a **pickled snapshot** | A read never touches SQLite; `pickle.loads` of a cached blob is an exact deep copy in C (6–8× faster than `copy.deepcopy`). Writes go through one lock, refresh the cache and drop the snapshot. Hot paths receive the dict once and pass it down (`settings=`); the per-order risk check and the news lock read one key (`config.setting`). |
| SQLite in WAL mode, `synchronous=NORMAL`, per-thread connections | Reads never block writes; no fsync per commit; the history writer runs on its own thread with a queue so order logging never waits on disk. |
| Pooled HTTP clients (`app/http.py`) | Keep-alive to Tradovate / gateways; no TLS handshake per order. |
| Per-login pacing with a **priority lane** for orders | Polls and health checks are spaced (0.2 s) so the broker never rate-limits; order / cancel / modify / liquidate calls use their own 60 ms lane and never queue behind a poll. |
| Copy trading: poll cadence adapts | 2 s while the socket is down, 10 s while the socket is synced (socket = accelerator), order list every second poll, 429 → back-off with the broker's `p-time`. |
| Copy trading: one leader snapshot per login (`app/leader_feed.py`) | Groups leading from accounts of the same login share the positions / orders snapshot: a group asking within 0.8 s reuses it, concurrent askers wait for the one request in flight (single-flight). N groups on one login cost one poll against its rate budget; rows are copied on the way out. |
| P&L poll: per-area parallel **with a per-area due time**, idle accounts every 6th tick, cached risk settings | One slow broker cannot hold the others; a workspace that is not due is skipped instead of polled at its neighbour's cadence; flat accounts cost nothing most ticks. |
| Health loop: per-session schedule with back-off | Each login renews when *its* token nears expiry; a login with a bad token retries 60 → 600 s on its own and no longer drags every login into a renewal per minute. |
| `/api/positions`, twin reconcile: one request per login | Accounts on the same login share one `/position/list` / `/order/list`; contract names are cached on the session. |
| Copy engine writes go through the history writer thread | A WAL commit for an event or state row never runs on the event loop between two mirror orders. |
| SSE for the dashboard, REST polling only where it must | Events, orders, signals, sessions and P&L are pushed; each frame is serialised once for all subscribers, a slow tab gets a `resync` marker instead of silently losing messages; pages poll status at 5–15 s, tables skip the rebuild when the rows did not change, the Logs page inserts new rows instead of repainting 200. |
| Ring buffers in memory (200 rows) + durable tables | The UI reads memory; the database is for restarts and history. |

## Hot-path rules (keep these when changing code)

1. **No `load_settings()` inside loops.** Load once per request / per poll and pass the
   dict down (`settings=`). Each call unpickles the workspace's settings (25–70 µs); a
   single key comes cheaper through `config.setting(key)`. `tests/test_perf.py` pins one
   read per signal.
2. **No SQLite call on the order path** except the async history queue. Risk locks live in
   memory (`risk_state` cached), the webhook token index is an in-memory dict rebuilt
   only when a webhook list changes.
3. **One broker request per fact.** Positions for a login are fetched once per tick and
   shared (P&L, watch, risk); contract ids are cached an hour; the order list is read once
   per copy poll.
4. **Never `await` a network call while holding a lock others need** unless the lock is the
   point (follower lock during a mirror order is intended: it serialises that account).
5. **Bound every cache and list** (`copy_events` pruned, `_closed_today` capped, twins
   verified, contract caches keyed by symbol, active-trade records swept after 14 days).
6. **Alerts and history writes are fire-and-forget** (`_fire`, the history thread). A slow
   SMTP server or disk never delays an order or a webhook answer.
7. **Frontend:** poll ≥ 5 s, compare JSON before touching the DOM, one toast per outage,
   reconnect SSE with back-off.

## Where the time goes per signal (bracket entry, measured)

| Step | Cost |
|---|---|
| Passphrase / news lock / trading switch / symbol map checks | ≈ 0.1 ms (in-memory) |
| Sizing per account, risk-lock check | ≈ 0.05 ms |
| Broker call(s) | broker latency × (entry + stop + targets), placed in the priority lane |
| Order log + SSE publish + history queue | ≈ 0.2 ms |

## Measuring

```bash
.venv2/bin/python scratchpad/bench_signal.py       # signals/s, load_settings per signal, per-caller attribution
.venv2/bin/python -m pytest -q tests/test_perf.py  # the guarantees above, as tests
.venv2/bin/python -m pytest -q tests/test_copy.py  # the copy engine under a 10 ms poll
```

`bench_signal.py` (kept outside the repo, in the session scratchpad) builds a workspace with
one bracket webhook and a fake executor, runs 300 entry + close pairs, counts
`load_settings` calls per caller and prints signals/s. Numbers above come from the shared
CI-class container; absolute figures vary, the ratios do not.

Watch in production: **Settings → Updates** shows the version; the Overview's connection
pill and the copy group's *latency* column show broker round trips; `journalctl -u
fluxbridge` (self-hosted) or Render logs show 429s (`rate limited`), which mean a poll
cadence is too aggressive for the number of logins.
