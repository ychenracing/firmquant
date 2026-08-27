from __future__ import annotations

from dataclasses import replace

import pytest

from firmquant.domain.broker_facts import BrokerOrderStatus, Side
from firmquant.observability.reports import DailyReportRenderer, OrderLifecycle
from tests.unit.observability.test_reports import report


@pytest.mark.parametrize(
    ("broker_status", "aggregate_state", "filled_shares"),
    [
        (BrokerOrderStatus.PENDING_NEW, "SUBMITTING", 0),
        (BrokerOrderStatus.ACKNOWLEDGED, "ACKNOWLEDGED", 0),
        (BrokerOrderStatus.PARTIALLY_FILLED, "PARTIALLY_FILLED", 40),
        (BrokerOrderStatus.FILLED, "FILLED", 100),
        (BrokerOrderStatus.PENDING_CANCEL, "CANCEL_REQUESTED", 40),
        (BrokerOrderStatus.CANCELLED, "CANCELLED", 40),
        (BrokerOrderStatus.REJECTED, "REJECTED", 40),
        (BrokerOrderStatus.EXPIRED, "EXPIRED", 40),
        (BrokerOrderStatus.UNKNOWN, "UNKNOWN", 40),
    ],
)
def test_report_exposes_true_broker_status_without_rewriting_aggregate_state(
    broker_status: BrokerOrderStatus,
    aggregate_state: str,
    filled_shares: int,
) -> None:
    lifecycle = OrderLifecycle(
        uquant_order_id="O-BROKER-STATUS",
        execution_id="execution-broker-status",
        broker_order_id="broker-status-order",
        symbol="300502.SZ",
        side=Side.BUY,
        requested_shares=100,
        filled_shares=filled_shares,
        state=aggregate_state,
        reason_code="BROKER_STATUS_EVIDENCE",
        broker_status=broker_status,
    )

    payload = lifecycle.payload()
    markdown = DailyReportRenderer().render_markdown(replace(report(), orders=(lifecycle,)))

    assert payload["state"] == aggregate_state
    assert payload["broker_status"] == broker_status.value
    assert f"broker={broker_status.value}" in markdown


def test_rejected_and_expired_broker_truth_are_never_reported_as_cancelled() -> None:
    for status in (BrokerOrderStatus.REJECTED, BrokerOrderStatus.EXPIRED):
        lifecycle = OrderLifecycle(
            uquant_order_id=f"O-{status.value}",
            execution_id=f"execution-{status.value.lower()}",
            broker_order_id=f"broker-{status.value.lower()}",
            symbol="300502.SZ",
            side=Side.BUY,
            requested_shares=100,
            filled_shares=40,
            state=status.value,
            reason_code=f"BROKER_{status.value}",
            broker_status=status,
        )

        assert lifecycle.payload()["broker_status"] == status.value
        assert lifecycle.payload()["broker_status"] != BrokerOrderStatus.CANCELLED.value
