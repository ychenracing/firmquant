from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from firmquant.broker.client_identity import client_order_tag
from firmquant.broker.xtquant_production import ProductionXtQuantBroker
from firmquant.domain.broker_facts import BrokerOrderStatus
from tests.fixtures.xtquant_sdk_fake import ContractXtQuantSdkFacade, SdkObject


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _broker() -> tuple[ProductionXtQuantBroker, ContractXtQuantSdkFacade, MutableClock]:
    facade = ContractXtQuantSdkFacade()
    clock = MutableClock(datetime(2026, 8, 25, 1, 31, tzinfo=UTC))
    broker = ProductionXtQuantBroker(facade=facade, account_id="account-001", clock=clock)
    broker.connect()
    return broker, facade, clock


def test_returned_order_price_type_does_not_reuse_submit_enum_and_identity_is_stable() -> None:
    broker, facade, clock = _broker()
    tag = client_order_tag("O000000123")
    facade.orders = [
        SdkObject(
            price_type=999,
            order_time=93001,
            order_remark=tag,
            order_status=50,
        )
    ]

    first = broker.query_orders()[0]
    clock.value = datetime(2026, 8, 25, 1, 40, tzinfo=UTC)
    second = broker.query_orders()[0]

    assert first.client_order_id == tag
    assert first.status is BrokerOrderStatus.ACKNOWLEDGED
    assert first.event_time.isoformat() == "2026-08-25T09:30:01+08:00"
    assert first.event_time == second.event_time
    assert first.event_sequence == second.event_sequence
    assert first.raw_payload_sha256 == second.raw_payload_sha256


def test_repeated_trade_query_uses_broker_trade_time_not_local_observation_time() -> None:
    broker, facade, clock = _broker()
    facade.trades = [
        SdkObject(
            traded_volume=100,
            traded_price=10.1,
            traded_time=93101,
            order_remark=client_order_tag("O000000123"),
        )
    ]

    first = broker.query_fills()[0]
    clock.value = datetime(2026, 8, 25, 1, 50, tzinfo=UTC)
    second = broker.query_fills()[0]

    assert first.event_time.isoformat() == "2026-08-25T09:31:01+08:00"
    assert first.event_time == second.event_time
    assert first.event_sequence == second.event_sequence
    assert first.raw_payload_sha256 == second.raw_payload_sha256


def test_duplicate_order_callbacks_keep_stable_event_identity() -> None:
    broker, facade, clock = _broker()
    received: list[dict[str, object]] = []
    broker.subscribe(received.append)
    raw = SdkObject(order_time=93001, order_remark=client_order_tag("O000000123"))

    facade.emit("ORDER", raw)
    clock.value = datetime(2026, 8, 25, 1, 45, tzinfo=UTC)
    facade.emit("ORDER", raw)

    assert len(received) == 2
    assert received[0] == received[1]


def test_order_cancel_errors_and_disconnect_are_forwarded_as_durable_operational_events() -> None:
    broker, facade, _ = _broker()
    received: list[dict[str, object]] = []
    broker.subscribe(received.append)
    tag = client_order_tag("O000000123")

    facade.emit(
        "ORDER_ERROR",
        SimpleNamespace(
            account_id="account-001",
            order_id=9001,
            error_id=31,
            error_msg="rejected",
            strategy_name="firmquant",
            order_remark=tag,
        ),
    )
    facade.emit(
        "CANCEL_ERROR",
        SimpleNamespace(
            account_id="account-001",
            order_id=9001,
            error_id=32,
            error_msg="cancel rejected",
        ),
    )
    facade.emit("DISCONNECTED", {})

    assert [event["event_type"] for event in received] == [
        "ORDER_ERROR",
        "CANCEL_ERROR",
        "DISCONNECTED",
    ]
    assert received[0]["payload"]["client_order_id"] == tag
    assert received[0]["payload"]["error_code"] == 31
    assert received[1]["payload"]["broker_order_id"] == "9001"
    assert broker.health().connected is False
