from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from firmquant.domain.broker_facts import AccountType
from firmquant.domain.values import Money
from firmquant.strategy.account_sync import StrategySyncError, to_uquant_broker_payload
from tests.fixtures.broker_snapshots import completed_buy_snapshot


def test_broker_snapshot_translates_to_exact_uquant_public_contract() -> None:
    payload = to_uquant_broker_payload(completed_buy_snapshot())

    assert payload == {
        "as_of": "2026-01-06",
        "cash": 994.9,
        "positions": [
            {
                "symbol": "sz300308",
                "shares": 100,
                "sellable_shares": 0,
                "avg_cost": 10.051,
            }
        ],
        "orders": [],
        "fills": [
            {
                "fill_id": "broker-fill-1",
                "order_id": "O000000001",
                "fill_date": "2026-01-06",
                "symbol": "sz300308",
                "side": "BUY",
                "shares": 100,
                "price": 10.0,
                "gross_value": 1000.0,
                "commission": 5.0,
                "stamp_duty": 0.0,
                "transfer_fee": 0.1,
                "slippage_cost": 0.0,
                "final": True,
                "remaining_shares": 0,
                "execution_sequence": 1,
            }
        ],
    }


def test_uquant_float_boundary_rejects_unrepresentable_decimal() -> None:
    snapshot = completed_buy_snapshot()
    unsafe = replace(
        snapshot.account,
        available_cash=Money(Decimal("9007199254740993")),
        total_assets=Money(Decimal("9007199254740993")),
    )

    with pytest.raises(StrategySyncError, match="cash cannot cross uquant float boundary"):
        to_uquant_broker_payload(replace(snapshot, account=unsafe))


def test_non_cash_account_and_unmapped_order_fail_closed() -> None:
    snapshot = completed_buy_snapshot()
    margin = replace(snapshot.account, account_type=AccountType.MARGIN)
    with pytest.raises(StrategySyncError, match="cash account"):
        to_uquant_broker_payload(replace(snapshot, account=margin))

    unmapped = replace(snapshot.orders[0], client_order_id=None)
    with pytest.raises(StrategySyncError, match="uquant order identity"):
        to_uquant_broker_payload(replace(snapshot, orders=(unmapped,)))
