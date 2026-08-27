from __future__ import annotations

from datetime import UTC, datetime

import pytest

from firmquant.broker.normalization import normalize_order
from firmquant.domain.broker_facts import BrokerOrderStatus

NOW = datetime(2026, 8, 25, 9, 31, tzinfo=UTC)


@pytest.mark.parametrize("status", tuple(BrokerOrderStatus))
def test_all_canonical_broker_order_statuses_survive_normalization(
    status: BrokerOrderStatus,
) -> None:
    order = normalize_order(
        {
            "broker_order_id": "broker-order-status-contract",
            "client_order_id": "uquant-order-status-contract",
            "symbol": "600519.SH",
            "side": "BUY",
            "price_type": "LIMIT",
            "status": status.value,
            "requested_shares": 100,
            "filled_shares": 0,
            "limit_price": "10.10",
            "session_date": "2026-08-25",
            "event_time": "2026-08-25T09:31:00+08:00",
            "event_sequence": 9,
        },
        received_at=NOW,
    )

    assert order.status is status
