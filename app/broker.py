"""The broker surface the rest of the bridge is allowed to use.

Everything above the broker — strategies, the copy engine, the risk guard, P&L,
the journal, rollover — talks to a login through :class:`BrokerSession` and to a
trade account through :class:`BrokerExecutor`. Today the only implementation is
:class:`app.tradovate.TradovateSession` / ``AccountExecutor``; a second broker
(see ``docs/RITHMIC.md``) implements these two protocols and nothing else in
the bridge changes. ``token_accounts[i]["broker"]`` selects the implementation
(default ``tradovate``); an unknown value yields no session, so an account can
never trade through a broker the bridge does not support.

The methods are *named* after what the caller needs, not after Tradovate's
endpoints, so an implementation may map them to whatever transport it has.
Tradovate-specific report access (the journal importer) is explicitly marked
``raw_get`` and raises ``NotImplementedError`` on other brokers.
"""
from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

BROKERS = ("tradovate", "rithmic")   # implementations the bridge ships


def broker_of(entry: dict[str, Any]) -> str:
    """The broker a login entry (``token_accounts[i]``) belongs to."""
    return str(entry.get("broker") or "tradovate").lower()


@runtime_checkable
class BrokerSession(Protocol):
    """One login at a broker: identity, accounts, connection state and the read
    feeds the engines poll. Attributes every implementation exposes:
    ``kind`` (broker name), ``idx``, ``name``, ``environment`` (demo|live),
    ``enabled``, ``accounts`` (list of ``{"spec", "id", "enabled", …}``),
    ``agent_id``, ``area_id``."""
    kind: str

    # ---- lifecycle
    async def connect(self) -> dict[str, Any]: ...
    async def health_check(self) -> dict[str, Any]: ...
    def has_token(self) -> bool: ...

    # ---- read feeds (raw broker rows; callers filter by account id)
    async def positions_snapshot(self) -> list[dict[str, Any]]:
        """Every position of the login: rows with ``accountId``, ``contractId``, ``netPos``."""
        ...
    async def orders_snapshot(self) -> list[dict[str, Any]]:
        """Every order of the login (all statuses): ``id``, ``accountId``, ``contractId``, ``action``, ``ordStatus``, ``ocoId``."""
        ...
    async def order_versions(self, order_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Latest version per order id: ``orderQty``, ``orderType``, ``price``, ``stopPrice``, ``id``."""
        ...
    async def account_list(self) -> list[dict[str, Any]]:
        """Accounts of the login as the broker lists them (``id``, ``name``)."""
        ...
    async def cash_snapshot(self, account_id: int) -> dict[str, Any]:
        """``totalCashValue``, ``realizedPnL``, ``openPnL``, ``weekRealizedPnL`` for one account."""
        ...
    async def auto_liq_rules(self) -> list[dict[str, Any]]:
        """Per-account risk / auto-liquidation records (trailing drawdown …); may be empty."""
        ...
    async def user_id(self) -> int:
        """The broker-side user id (0 when unknown)."""
        ...

    # ---- contracts
    async def contract_info(self, contract_id: int) -> dict[str, Any]: ...
    async def contract_find(self, name: str) -> dict[str, Any]: ...
    async def contract_suggest(self, root: str, limit: int = 30) -> list[dict[str, Any]]: ...
    async def contract_maturity(self, maturity_id: int) -> dict[str, Any]: ...
    async def resolve_contract(self, root_or_symbol: str) -> str: ...
    async def contract_id(self, symbol: str) -> int: ...

    # ---- broker-specific reports (journal importer)
    async def raw_get(self, path: str, *, params: Optional[dict[str, Any]] = None) -> Any:
        """A GET on a broker-specific report endpoint. Tradovate only."""
        ...


@runtime_checkable
class BrokerExecutor(Protocol):
    """One trade account: what the strategies and the copy engine place orders through."""
    name: str
    spec: str
    id: int
    session: Any

    async def resolve_contract(self, root_or_symbol: str) -> str: ...
    async def contract_id(self, symbol: str) -> int: ...
    async def place_order(self, **kw: Any) -> dict[str, Any]: ...
    async def place_oco(self, **kw: Any) -> dict[str, Any]: ...
    async def modify_order(self, order_id: int, **kw: Any) -> dict[str, Any]: ...
    async def cancel_order(self, order_id: int) -> dict[str, Any]: ...
    async def working_orders(self) -> list[dict[str, Any]]: ...
    async def order_versions(self, order_ids: list[int]) -> dict[int, dict[str, Any]]: ...
    async def liquidate_position(self, symbol: str) -> dict[str, Any]: ...
    async def positions(self) -> list[dict[str, Any]]: ...
