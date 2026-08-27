from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from firmquant.domain.broker_facts import BrokerOrderStatus
from firmquant.domain.values import Money, Price, Shares
from firmquant.strategy.account_sync import StrategySyncError, sync_account, to_uquant_broker_payload
from tests.fixtures.broker_snapshots import (
    cancelled_buy_snapshot,
    completed_buy_snapshot,
    open_buy_account,
)


@pytest.mark.parametrize(
    "status",
    [
        BrokerOrderStatus.CANCELLED,
        BrokerOrderStatus.REJECTED,
        BrokerOrderStatus.EXPIRED,
    ],
)
def test_terminal_broker_status_projects_only_no_active_remainder_to_uquant(
    status: BrokerOrderStatus,
) -> None:
    snapshot = cancelled_buy_snapshot()
    snapshot = replace(snapshot, orders=(replace(snapshot.orders[0], status=status),))

    payload = to_uquant_broker_payload(snapshot)

    assert snapshot.orders[0].status is status
    assert payload["orders"] == [
        {
            "order_id": "O000000001",
            "status": "CANCELLED",
            "remaining_shares": 0,
        }
    ]


def _partial_terminal_snapshot(status: BrokerOrderStatus):
    completed = completed_buy_snapshot()
    order = replace(
        completed.orders[0],
        status=status,
        filled_shares=Shares(40),
    )
    fill = replace(completed.fills[0], shares=Shares(40))
    position = replace(
        completed.positions[0],
        total_shares=Shares(40),
        sellable_shares=Shares(0),
        average_cost=Price(Decimal("10.1275")),
        market_value=Money(Decimal("400")),
    )
    return replace(
        completed,
        snapshot_id=f"snapshot-partial-{status.value.lower()}",
        account=replace(
            completed.account,
            available_cash=Money(Decimal("1594.9")),
            total_assets=Money(Decimal("1994.9")),
        ),
        positions=(position,),
        orders=(order,),
        fills=(fill,),
        raw_payload_sha256="7" * 64,
    )


@pytest.mark.parametrize(
    "status",
    [BrokerOrderStatus.REJECTED, BrokerOrderStatus.EXPIRED, BrokerOrderStatus.CANCELLED],
)
def test_partial_terminal_sync_imports_fill_then_closes_remainder_with_exact_economics(
    status: BrokerOrderStatus,
) -> None:
    account = open_buy_account()
    snapshot = _partial_terminal_snapshot(status)

    payload = to_uquant_broker_payload(snapshot)
    assert payload["fills"] == [
        {
            "fill_id": "broker-fill-1",
            "order_id": "O000000001",
            "fill_date": "2026-01-06",
            "symbol": "sz300308",
            "side": "BUY",
            "shares": 40,
            "price": 10.0,
            "gross_value": 400.0,
            "commission": 5.0,
            "stamp_duty": 0.0,
            "transfer_fee": 0.1,
            "slippage_cost": 0.0,
            "final": False,
            "remaining_shares": 60,
            "execution_sequence": 1,
        }
    ]
    assert payload["orders"] == [
        {
            "order_id": "O000000001",
            "status": "CANCELLED",
            "remaining_shares": 0,
        }
    ]

    receipt = sync_account(account, snapshot)

    assert receipt.fills_imported == 1
    assert receipt.pending_orders == 0
    assert account.pending_orders == []
    assert account.order_ledger[0].status == "CANCELLED"
    assert account.order_ledger[0].filled_shares == 40
    assert account.cash == 1594.9
    assert account.positions["sz300308"].shares == 40
    fill = account.fills[0]
    assert fill.shares == 40
    assert fill.gross_value == 400.0
    assert fill.commission == 5.0
    assert fill.transfer_fee == 0.1
    assert snapshot.orders[0].status is status


def test_unknown_broker_status_is_never_projected_as_closed() -> None:
    snapshot = cancelled_buy_snapshot()
    unknown = replace(snapshot.orders[0], status=BrokerOrderStatus.UNKNOWN)

    with pytest.raises(StrategySyncError, match="UNKNOWN"):
        to_uquant_broker_payload(replace(snapshot, orders=(unknown,)))
