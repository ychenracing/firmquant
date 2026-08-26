from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import tests.unit.broker.test_production_xtquant as base
from firmquant.broker.client_identity import client_order_tag
from firmquant.broker.gateway import (
    BrokerOrderCommand,
    BrokerWriteForbidden,
    _broker_write_authorization_scope,
)
from firmquant.broker.xtquant import BrokerSchemaMismatch
from firmquant.broker.xtquant_production import (
    _broker_time,
    _client_tag,
    _fill_sequence,
    _order_sequence,
)
from firmquant.domain.broker_facts import BrokerOrderStatus, PriceType, Side
from firmquant.domain.values import Price, Shares, Symbol
from firmquant.execution.write_outcome import BrokerWriteNotAccepted, BrokerWriteOutcomeUnknown
from tests.fixtures.xtquant_sdk_fake import SdkObject

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _command(*, side: Side = Side.BUY) -> BrokerOrderCommand:
    return BrokerOrderCommand(
        execution_id="exec_" + "a" * 64,
        idempotency_key="b" * 64,
        client_order_id="O000000123",
        symbol=Symbol.parse("sh600519"),
        side=side,
        price_type=PriceType.LIMIT,
        requested_shares=Shares(100),
        limit_price=Price("10.10"),
        strategy_session=datetime(2026, 8, 24, tzinfo=UTC).date(),
    )


def test_broker_time_accepts_documented_shapes_and_is_stable() -> None:
    observed = datetime(2026, 8, 25, 1, 31, tzinfo=UTC)
    expected = datetime(2026, 8, 25, 9, 30, 1, tzinfo=_SHANGHAI)
    epoch = int(expected.timestamp())

    assert _broker_time(93001, observed_at=observed, label="t") == expected
    assert _broker_time("93001", observed_at=observed, label="t") == expected
    assert _broker_time(20260825093001, observed_at=observed, label="t") == expected
    assert _broker_time("20260825093001", observed_at=observed, label="t") == expected
    assert _broker_time(epoch, observed_at=observed, label="t") == expected
    assert _broker_time(epoch * 1000, observed_at=observed, label="t") == expected
    assert _broker_time("2026-08-25T09:30:01", observed_at=observed, label="t") == expected
    assert _broker_time("2026-08-25T01:30:01+00:00", observed_at=observed, label="t") == expected
    assert _fill_sequence(expected) == int(expected.timestamp() * 1_000_000)
    assert _order_sequence(BrokerOrderStatus.FILLED, 100) > _order_sequence(
        BrokerOrderStatus.ACKNOWLEDGED, 100
    )


@pytest.mark.parametrize(
    "value",
    [True, -1, "", "not-time", 930099, "20261325093001", object()],
)
def test_broker_time_rejects_ambiguous_or_invalid_shapes(value: object) -> None:
    with pytest.raises(BrokerSchemaMismatch):
        _broker_time(value, observed_at=datetime(2026, 8, 25, 1, 31, tzinfo=UTC), label="t")


def test_client_tag_and_order_fill_payload_validation() -> None:
    tag = client_order_tag("O000000123")
    assert _client_tag({"order_remark": tag}) == tag
    assert _client_tag(SimpleNamespace(order_remark=tag)) == tag
    assert _client_tag({"order_remark": "manual"}) is None
    assert _client_tag(object()) is None

    broker, facade, _clock = base._broker()
    with pytest.raises(BrokerSchemaMismatch, match="exceeds"):
        broker._order_payload(
            SdkObject(order_volume=100, traded_volume=101, order_remark=tag),
            observed_at=datetime(2026, 8, 25, 1, 31, tzinfo=UTC),
        )

    facade.fees = object()  # type: ignore[assignment]
    with pytest.raises(BrokerSchemaMismatch, match="fee provider"):
        broker._fill_payload(
            SdkObject(traded_volume=100, traded_price=10.1, traded_time=93101),
            observed_at=datetime(2026, 8, 25, 1, 31, tzinfo=UTC),
        )


def test_query_orders_and_fills_are_sorted_by_stable_broker_identity() -> None:
    broker, facade, _clock = base._broker()
    tag1 = client_order_tag("O000000123")
    tag2 = client_order_tag("O000000124")
    facade.orders = [
        SdkObject(order_id=9002, order_time=93002, order_remark=tag2),
        SdkObject(order_id=9001, order_time=93001, order_remark=tag1),
    ]
    assert [item.broker_order_id for item in broker.query_orders()] == ["9001", "9002"]

    facade.trades = [
        SdkObject(traded_id="fill-2", order_id=9002, traded_time=93102),
        SdkObject(traded_id="fill-1", order_id=9001, traded_time=93101),
    ]
    assert [item.broker_fill_id for item in broker.query_fills()] == ["fill-1", "fill-2"]


def test_submit_outcome_classification_covers_every_broker_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, facade, _clock = base._broker()
    command = _command()

    with pytest.raises(BrokerWriteForbidden):
        broker.submit_order(command)
    with _broker_write_authorization_scope(), pytest.raises(TypeError):
        broker.submit_order(object())  # type: ignore[arg-type]

    monkeypatch.setattr(facade, "order_stock", lambda *_args: (_ for _ in ()).throw(RuntimeError("x")))
    with _broker_write_authorization_scope(), pytest.raises(BrokerWriteOutcomeUnknown, match="call outcome"):
        broker.submit_order(command)

    monkeypatch.setattr(facade, "order_stock", lambda *_args: True)
    with _broker_write_authorization_scope(), pytest.raises(BrokerWriteOutcomeUnknown, match="identity"):
        broker.submit_order(command)

    monkeypatch.setattr(facade, "order_stock", lambda *_args: 0)
    with _broker_write_authorization_scope(), pytest.raises(BrokerWriteNotAccepted):
        broker.submit_order(command)

    monkeypatch.setattr(facade, "order_stock", lambda *_args: 9001)
    monkeypatch.setattr(
        facade,
        "query_stock_order",
        lambda _order_id: (_ for _ in ()).throw(RuntimeError("query")),
    )
    with _broker_write_authorization_scope(), pytest.raises(BrokerWriteOutcomeUnknown, match="query failed"):
        broker.submit_order(command)

    monkeypatch.setattr(facade, "query_stock_order", lambda _order_id: None)
    with _broker_write_authorization_scope(), pytest.raises(BrokerWriteOutcomeUnknown, match="not yet queryable"):
        broker.submit_order(command)

    monkeypatch.setattr(facade, "query_stock_order", lambda _order_id: object())
    with _broker_write_authorization_scope(), pytest.raises(BrokerWriteOutcomeUnknown, match="fact is invalid"):
        broker.submit_order(command)

    wrong = SdkObject(order_id=9002, order_remark=client_order_tag(command.client_order_id))
    monkeypatch.setattr(facade, "query_stock_order", lambda _order_id: wrong)
    with _broker_write_authorization_scope(), pytest.raises(BrokerWriteOutcomeUnknown, match="identity cannot be proven"):
        broker.submit_order(command)

    accepted = SdkObject(
        order_id=9001,
        order_type=facade.constants.stock_buy,
        order_remark=client_order_tag(command.client_order_id),
        order_volume=command.requested_shares.value,
        price=10.1,
    )
    monkeypatch.setattr(facade, "query_stock_order", lambda _order_id: accepted)
    with _broker_write_authorization_scope():
        fact = broker.submit_order(command)
    assert fact.broker_order_id == "9001"
    assert fact.client_order_id == client_order_tag(command.client_order_id)


def test_cancel_outcome_classification_covers_every_broker_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, facade, _clock = base._broker()

    with pytest.raises(BrokerWriteForbidden):
        broker.cancel_order("9001")
    with _broker_write_authorization_scope(), pytest.raises(BrokerSchemaMismatch):
        broker.cancel_order("invalid")

    monkeypatch.setattr(
        facade,
        "cancel_order_stock",
        lambda _order_id: (_ for _ in ()).throw(RuntimeError("cancel")),
    )
    with _broker_write_authorization_scope(), pytest.raises(BrokerWriteOutcomeUnknown, match="call outcome"):
        broker.cancel_order("9001")

    monkeypatch.setattr(facade, "cancel_order_stock", lambda _order_id: True)
    with _broker_write_authorization_scope(), pytest.raises(BrokerWriteOutcomeUnknown, match="invalid result"):
        broker.cancel_order("9001")

    monkeypatch.setattr(facade, "cancel_order_stock", lambda _order_id: 1)
    with _broker_write_authorization_scope(), pytest.raises(BrokerWriteNotAccepted):
        broker.cancel_order("9001")

    monkeypatch.setattr(facade, "cancel_order_stock", lambda _order_id: 0)
    monkeypatch.setattr(
        facade,
        "query_stock_order",
        lambda _order_id: (_ for _ in ()).throw(RuntimeError("query")),
    )
    with _broker_write_authorization_scope(), pytest.raises(BrokerWriteOutcomeUnknown, match="query failed"):
        broker.cancel_order("9001")

    monkeypatch.setattr(facade, "query_stock_order", lambda _order_id: None)
    with _broker_write_authorization_scope(), pytest.raises(BrokerWriteOutcomeUnknown, match="not queryable"):
        broker.cancel_order("9001")

    monkeypatch.setattr(facade, "query_stock_order", lambda _order_id: object())
    with _broker_write_authorization_scope(), pytest.raises(BrokerWriteOutcomeUnknown, match="fact is invalid"):
        broker.cancel_order("9001")

    cancelled = SdkObject(order_id=9001, order_status=facade.constants.order_canceled)
    monkeypatch.setattr(facade, "query_stock_order", lambda _order_id: cancelled)
    with _broker_write_authorization_scope():
        fact = broker.cancel_order("9001")
    assert fact.status is BrokerOrderStatus.CANCELLED


def test_operational_callbacks_reject_bad_identity_and_surface_sink_failure() -> None:
    broker, facade, _clock = base._broker()
    received: list[dict[str, object]] = []
    broker.subscribe(received.append)

    facade.emit("UNSUPPORTED", {})
    assert broker.health().diagnostic_code == "CALLBACK_SCHEMA_INVALID"
    assert received == []

    facade.emit(
        "ORDER_ERROR",
        SimpleNamespace(
            account_id="wrong-account",
            order_id=1,
            error_id=2,
            error_msg="bad",
            order_remark=client_order_tag("O000000123"),
        ),
    )
    assert broker.health().diagnostic_code == "CALLBACK_SCHEMA_INVALID"

    broker.subscribe(lambda _event: (_ for _ in ()).throw(RuntimeError("sink")))
    with pytest.raises(RuntimeError, match="sink"):
        facade.emit("DISCONNECTED", {})
    assert broker.health().diagnostic_code == "CALLBACK_SINK_FAILED"


def test_operational_error_payload_handles_unbound_cancel_and_negative_order_id() -> None:
    broker, _facade, _clock = base._broker()
    payload = broker._operational_error_payload(
        "CANCEL_ERROR",
        SimpleNamespace(
            account_id="account-001",
            order_id=-1,
            error_id=-5,
            error_msg="cancel failed",
        ),
    )
    assert payload["broker_order_id"] is None
    assert payload["client_order_id"] is None
    assert payload["error_code"] == -5
    assert broker._operational_event_id("CANCEL_ERROR", {"order": 1}).startswith(
        "xtquant-cancel_error-"
    )
