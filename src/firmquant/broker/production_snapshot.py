"""Bounded production snapshot collection that tolerates mark-to-market movement only."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from firmquant.domain.broker_facts import (
    BrokerAccountFact,
    BrokerFillFact,
    BrokerOrderFact,
    BrokerPositionFact,
    BrokerSnapshot,
)
from firmquant.persistence.repositories import canonical_json

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class ProductionSnapshotUnstable(RuntimeError):
    """No bounded read window proved a coherent set of economic broker facts."""


@runtime_checkable
class ProductionSnapshotReadPort(Protocol):
    def query_account(self) -> BrokerAccountFact: ...

    def query_positions(self) -> tuple[BrokerPositionFact, ...]: ...

    def query_orders(self) -> tuple[BrokerOrderFact, ...]: ...

    def query_fills(self) -> tuple[BrokerFillFact, ...]: ...


@dataclass(frozen=True, slots=True)
class _Read:
    account: BrokerAccountFact
    positions: tuple[BrokerPositionFact, ...]
    orders: tuple[BrokerOrderFact, ...]
    fills: tuple[BrokerFillFact, ...]


def _quantity_signature(read: _Read) -> str:
    """Exclude mark-to-market values but include every quantity/lifecycle authority fact."""

    return hashlib.sha256(
        canonical_json(
            {
                "account": {
                    "account_id_hash": read.account.account_id_hash,
                    "account_type": read.account.account_type,
                    "available_cash": read.account.available_cash,
                },
                "positions": [
                    {
                        "symbol": item.symbol,
                        "total_shares": item.total_shares,
                        "sellable_shares": item.sellable_shares,
                        "average_cost": item.average_cost,
                    }
                    for item in sorted(read.positions, key=lambda value: value.symbol.canonical)
                ],
                "orders": [
                    {
                        "broker_order_id": item.broker_order_id,
                        "client_order_id": item.client_order_id,
                        "symbol": item.symbol,
                        "side": item.side,
                        "status": item.status,
                        "requested_shares": item.requested_shares,
                        "filled_shares": item.filled_shares,
                        "limit_price": item.limit_price,
                        "event_sequence": item.event_sequence,
                    }
                    for item in sorted(read.orders, key=lambda value: value.broker_order_id)
                ],
                "fills": [
                    {
                        "broker_fill_id": item.broker_fill_id,
                        "broker_order_id": item.broker_order_id,
                        "symbol": item.symbol,
                        "side": item.side,
                        "shares": item.shares,
                        "price": item.price,
                        "commission": item.commission,
                        "stamp_duty": item.stamp_duty,
                        "transfer_fee": item.transfer_fee,
                        "event_sequence": item.event_sequence,
                    }
                    for item in sorted(read.fills, key=lambda value: value.broker_fill_id)
                ],
            }
        ).encode("utf-8")
    ).hexdigest()


def _snapshot_payload(read: _Read, *, captured_at: datetime) -> dict[str, object]:
    return {
        "schema": "firmquant.production-broker-snapshot.v1",
        "captured_at": captured_at,
        "account": {
            "account_id_hash": read.account.account_id_hash,
            "account_type": read.account.account_type,
            "available_cash": read.account.available_cash,
            "total_assets": read.account.total_assets,
        },
        "positions": [
            {
                "symbol": item.symbol,
                "total_shares": item.total_shares,
                "sellable_shares": item.sellable_shares,
                "average_cost": item.average_cost,
                "market_value": item.market_value,
            }
            for item in sorted(read.positions, key=lambda value: value.symbol.canonical)
        ],
        "order_payload_sha256": [
            item.raw_payload_sha256 for item in sorted(read.orders, key=lambda value: value.broker_order_id)
        ],
        "fill_payload_sha256": [
            item.raw_payload_sha256 for item in sorted(read.fills, key=lambda value: value.broker_fill_id)
        ],
        "quantity_signature_sha256": _quantity_signature(read),
    }


class ProductionSnapshotCollector:
    """Retry bounded reads until economic quantities stabilize; never freeze market marks."""

    def __init__(
        self,
        *,
        broker: ProductionSnapshotReadPort,
        clock: Callable[[], datetime],
        max_attempts: int = 3,
    ) -> None:
        if not isinstance(broker, ProductionSnapshotReadPort):
            raise TypeError("production snapshot broker does not satisfy read contract")
        if not callable(clock):
            raise TypeError("production snapshot clock must be callable")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise TypeError("production snapshot maximum attempts must be integer")
        if not 1 <= max_attempts <= 10:
            raise ValueError("production snapshot maximum attempts must be between one and ten")
        self._broker = broker
        self._clock = clock
        self._max_attempts = max_attempts

    def _read(self) -> _Read:
        return _Read(
            account=self._broker.query_account(),
            positions=tuple(self._broker.query_positions()),
            orders=tuple(self._broker.query_orders()),
            fills=tuple(self._broker.query_fills()),
        )

    def capture(self) -> BrokerSnapshot:
        for _attempt in range(self._max_attempts):
            first = self._read()
            second = self._read()
            if (
                first.account.account_id_hash != second.account.account_id_hash
                or first.account.account_type is not second.account.account_type
            ):
                raise ProductionSnapshotUnstable("broker account identity changed during snapshot")
            if _quantity_signature(first) != _quantity_signature(second):
                continue
            captured_at = self._clock()
            if captured_at.tzinfo is None or captured_at.utcoffset() is None:
                raise ProductionSnapshotUnstable("production snapshot clock is not timezone-aware")
            payload = _snapshot_payload(second, captured_at=captured_at)
            raw_payload_sha256 = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
            watermark = max(
                (
                    *(item.event_sequence for item in second.orders),
                    *(item.event_sequence for item in second.fills),
                    0,
                )
            )
            return BrokerSnapshot(
                snapshot_id="broker-snapshot-" + raw_payload_sha256,
                account=second.account,
                positions=tuple(sorted(second.positions, key=lambda value: value.symbol.canonical)),
                orders=tuple(sorted(second.orders, key=lambda value: value.broker_order_id)),
                fills=tuple(sorted(second.fills, key=lambda value: value.broker_fill_id)),
                session_date=captured_at.astimezone(_SHANGHAI).date(),
                captured_at=captured_at,
                broker_event_watermark=watermark,
                raw_payload_sha256=raw_payload_sha256,
                complete=True,
            )
        raise ProductionSnapshotUnstable("broker facts did not stabilize within bounded attempts")


__all__ = (
    "ProductionSnapshotCollector",
    "ProductionSnapshotReadPort",
    "ProductionSnapshotUnstable",
)
