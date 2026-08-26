from __future__ import annotations

from datetime import UTC, datetime

from firmquant.broker.normalization import (
    BrokerEventType,
    BrokerOperationalFact,
    normalize_broker_event,
)

NOW = datetime(2026, 8, 25, 1, 31, tzinfo=UTC)


def test_order_error_normalizes_to_typed_durable_operational_fact() -> None:
    event = normalize_broker_event(
        {
            "event_id": "xtquant-order_error-" + "a" * 64,
            "event_type": "ORDER_ERROR",
            "payload": {
                "broker_order_id": "9001",
                "client_order_id": "fq" + "b" * 22,
                "error_code": 31,
                "message_sha256": "c" * 64,
                "session_date": "2026-08-25",
                "event_time": "2026-08-25T09:31:00+08:00",
            },
        },
        received_at=NOW,
    )

    assert event.event_type is BrokerEventType.ORDER_ERROR
    assert event.broker_sequence == 0
    assert isinstance(event.fact, BrokerOperationalFact)
    assert event.fact.error_code == 31
    assert event.fact.client_order_id == "fq" + "b" * 22
    assert event.safe_payload["message_sha256"] == "c" * 64


def test_disconnect_normalizes_without_inventing_order_identity() -> None:
    event = normalize_broker_event(
        {
            "event_id": "xtquant-disconnected-" + "d" * 64,
            "event_type": "DISCONNECTED",
            "payload": {
                "session_date": "2026-08-25",
                "event_time": "2026-08-25T09:32:00+08:00",
            },
        },
        received_at=NOW,
    )

    assert event.event_type is BrokerEventType.DISCONNECTED
    assert isinstance(event.fact, BrokerOperationalFact)
    assert event.fact.broker_order_id is None
    assert event.fact.client_order_id is None
    assert event.fact.error_code is None
