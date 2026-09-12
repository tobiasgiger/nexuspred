"""Copy trading: mirror a leader account's positions onto follower accounts.

A **copy group** names one leader trade account, an optional symbol filter and
a list of followers with their sizing. The engine is a *position mirror*: on
every leader position change it computes each follower's target net position
and sends one market order for the difference to the follower's current
position. That single rule covers opening, adding, reducing, closing and
reversing, and it is self-healing — a missed event, a partial fill or a
rejected order is corrected on the next event or by the periodic reconcile.

**Feed.** The leader login is watched over Tradovate's WebSocket "user sync"
(events ~100 ms after a fill). A leader login that executes through a paired
agent, or a group set to ``feed: "poll"``, is polled once a second instead —
through the agent, so the login's IP rule is kept. Losing the feed for longer
than ``feed_loss_flatten_s`` flattens the followers' mirrored contracts and
pauses the group until the user resumes it.

**Sizing.** ``multiplier``: target = leader net × factor (rounded). ``fixed``:
``fixed`` contracts for the leader's initial entry, scaled proportionally when
the leader adds or reduces (``copy_adds``), or constant when not. Both are
capped by ``max_contracts`` and filtered by ``direction``.

**Baseline.** A leader position that already exists when the group starts is
not copied; mirroring of that contract begins once the leader is flat again,
or immediately after *Sync now*.
"""
from __future__ import annotations

from . import feed, groups, group_runner, manager, orders  # noqa: F401
from .groups import (  # noqa: F401
    _runners,
    new_group,
    normalize_follower,
    load_groups,
    save_groups,
    validate_group,
    _round_half_up,
    target_qty,
    parse_frames,
    sharing_of,
    public_view,
    published_groups,
    find_published,
    leader_broker,
    clean_subscriber_accounts,
    following_status,
    masked_status,
    external_followers,
    effective_followers,
)
from .group_runner import (  # noqa: F401
    WS_URLS,
    POLL_INTERVAL_S,
    POLL_WS_INTERVAL_S,
    RECONCILE_INTERVAL_S,
    HEARTBEAT_S,
    RECONNECT_BACKOFF_S,
    FEED_STALE_S,
    POLL_ERROR_SLEEP_S,
    POLL_MAX_INTERVAL_S,
    POLL_RECOVER_S,
    ORDERS_EVERY_N,
    WS_SYNC_TIMEOUT_S,
    WS_RECENT_MAX,
    REJECT_HOLDOFF_S,
    ORDER_SETTLE_S,
    FOLLOWER_RESEED_S,
    GroupRunner,
)
from .manager import (  # noqa: F401
    reset,
    release_followers,
    _enabled_specs,
    _sync_locks,
    sync_area,
    runner,
    statuses,
    _last_loop_error,
    copy_loop,
    stop_all,
)
from .orders import OrderMirror  # noqa: F401
