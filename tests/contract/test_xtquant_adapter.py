from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from firmquant.broker.gateway import (
    BrokerDisconnected,
    BrokerFactUnavailable,
    BrokerGateway,
    BrokerWriteForbidden,
)
from firmquant.broker.normalization import normalize_broker_event
from firmquant.broker.xtquant import BrokerSchemaMismatch, XtQuantBroker
from firmquant.domain.broker_facts import (
    AccountType,
    BrokerOrderStatus,
    MarketSessionStatus,
    Side,
)
from firmquant.domain.values import Shares, Symbol
from tests.fixtures.broker_contract import order_command
from tests.fixtures.xtquant_sdk_fake import ContractXtQuantSdkFacade, SdkObject

NOW = datetime(2026, 8, 25, 1, 31, tzinfo=UTC)


def broker(
    facade: ContractXtQuantSdkFacade | None = None,
) -> tuple[XtQuantBroker, ContractXtQuantSdkFacade]:
    selected = facade or ContractXtQuantSdkFacade()
    return (
        XtQuantBroker(
            facade=selected,
            account_id="account-001",
            clock=lambda: NOW,
        ),
        selected,
    )


def test_contract_fake_maps_official_read_fields_to_strict_domain_facts() -> None:
    gateway, facade = broker()
    facade.positions = [SdkObject()]
    facade.orders = [SdkObject()]
    facade.trades = [
        replace(
            SdkObject(),
            traded_volume=100,
            traded_price=10.1,
        )
    ]

    assert isinstance(gateway, BrokerGateway)
    assert gateway.health().connected is False
    gateway.connect()

    account = gateway.query_account()
    assert account.account_id_hash == hashlib.sha256(b"account-001").hexdigest()
    assert account.account_type is AccountType.CASH
    assert account.available_cash.canonical == "100000"

    position = gateway.query_positions()[0]
    assert position.symbol == Symbol.parse("600519.SH")
    assert position.total_shares == Shares(100)
    assert position.sellable_shares == Shares(80)

    order = gateway.query_orders()[0]
    assert order.broker_order_id == "9001"
    assert order.side is Side.BUY
    assert order.status is BrokerOrderStatus.ACKNOWLEDGED
    assert order.client_order_id is None

    fill = gateway.query_fills()[0]
    assert fill.broker_fill_id == "fill-1"
    assert fill.total_fees.canonical == "1.52"

    symbol = Symbol.parse("600519.SH")
    instrument = gateway.query_instrument(symbol)
    assert instrument.trading_unit == Shares(100)
    assert instrument.lower_limit is not None
    assert instrument.lower_limit.canonical == "9"

    quote = gateway.query_quote(symbol)
    assert quote.market_status is MarketSessionStatus.OPEN
    assert quote.volume == Shares(100_000)
    assert quote.event_time.isoformat() == "2026-08-25T09:31:00+08:00"
    assert gateway.query_market_status() is MarketSessionStatus.OPEN

    gateway.disconnect()
    assert facade.stopped is True
    with pytest.raises(BrokerDisconnected):
        gateway.query_account()


def test_connection_or_subscription_failure_stops_sdk_and_fails_closed() -> None:
    connect_failure = ContractXtQuantSdkFacade()
    connect_failure.connect_result = -1
    failed_connect, _ = broker(connect_failure)
    with pytest.raises(BrokerDisconnected, match="connect"):
        failed_connect.connect()
    assert connect_failure.stopped is True

    subscription_failure = ContractXtQuantSdkFacade()
    subscription_failure.subscribe_result = -1
    failed_subscription, _ = broker(subscription_failure)
    with pytest.raises(BrokerDisconnected, match="subscribe"):
        failed_subscription.connect()
    assert subscription_failure.stopped is True


def test_account_identity_and_account_type_mismatch_never_leak_identity() -> None:
    gateway, facade = broker()
    facade.asset = replace(SdkObject(), account_id="different-sensitive-account")
    gateway.connect()

    with pytest.raises(BrokerSchemaMismatch) as captured:
        gateway.query_account()
    assert "different-sensitive-account" not in str(captured.value)

    facade.asset = replace(SdkObject(), account_type=3)
    with pytest.raises(BrokerSchemaMismatch, match="cash securities account"):
        gateway.query_account()


def test_missing_authoritative_safety_fact_blocks_read_instead_of_guessing() -> None:
    gateway, facade = broker()
    gateway.connect()

    def unavailable() -> MarketSessionStatus:
        raise BrokerFactUnavailable("market status not verified")

    facade.market_status = unavailable  # type: ignore[method-assign]
    with pytest.raises(BrokerFactUnavailable, match="not verified"):
        gateway.query_market_status()
    with pytest.raises(BrokerFactUnavailable, match="not verified"):
        gateway.query_quote(Symbol.parse("600519.SH"))


def test_direct_xtquant_writes_are_forbidden_before_sdk_side_effect() -> None:
    gateway, facade = broker()
    gateway.connect()

    with pytest.raises(BrokerWriteForbidden, match="BrokerWriteCapability"):
        gateway.submit_order(order_command())
    with pytest.raises(BrokerWriteForbidden, match="BrokerWriteCapability"):
        gateway.cancel_order("9001")

    assert facade.order_calls == []
    assert facade.cancel_calls == []


def test_callbacks_are_mapped_then_enqueued_without_economic_state_mutation() -> None:
    gateway, facade = broker()
    received: list[dict[str, object]] = []
    gateway.subscribe(received.append)
    gateway.connect()

    raw = SdkObject()
    facade.emit("ORDER", raw)
    facade.emit("ORDER", raw)

    assert len(received) == 2
    assert received[0] == received[1]
    envelope = normalize_broker_event(received[0], received_at=NOW)
    assert envelope.fact.broker_order_id == "9001"
    assert gateway.query_orders() == ()


def test_invalid_callback_is_contained_and_marks_health_unhealthy() -> None:
    gateway, facade = broker()
    received: list[dict[str, object]] = []
    gateway.subscribe(received.append)
    gateway.connect()

    facade.emit("ORDER", object())

    assert received == []
    health = gateway.health()
    assert health.connected is True
    assert health.read_healthy is False
    assert health.write_healthy is False
    assert health.diagnostic_code == "CALLBACK_SCHEMA_INVALID"
