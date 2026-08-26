from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import fields, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from firmquant.broker.gateway import (
    BrokerDisconnected,
    BrokerFactUnavailable,
)
from firmquant.broker.xtquant import (
    BrokerDependencyMissing,
    BrokerSchemaMismatch,
    XtQuantBroker,
    XtQuantConstants,
    XtQuantFeeBreakdown,
    XtQuantInstrumentSafety,
    _decimal,
    _decimal_text,
    _field,
    _int,
    _mapping,
    _module_attribute,
    _optional_price_text,
    _safe_sequence,
    _signed_int,
    _text,
    diagnose_xtquant_sdk,
)
from firmquant.domain.broker_facts import (
    BrokerOrderStatus,
    MarketSessionStatus,
    SecurityStatus,
    SecurityType,
)
from firmquant.domain.errors import DomainTypeError, DomainValidationError
from firmquant.domain.values import Money, Shares, Symbol
from tests.contract.test_xtquant_adapter import NOW, broker
from tests.fixtures.xtquant_sdk_fake import (
    ContractXtQuantSdkFacade,
    OfficialSdkModules,
    SdkObject,
    full_tick,
)


def _constants() -> XtQuantConstants:
    return ContractXtQuantSdkFacade().constants


def test_xtquant_contract_value_objects_reject_guessed_safety_facts() -> None:
    constants = _constants()
    for field in fields(constants):
        with pytest.raises(DomainTypeError):
            replace(constants, **{field.name: True})
    with pytest.raises(DomainValidationError, match="distinct"):
        replace(constants, stock_sell=constants.stock_buy)

    with pytest.raises(DomainTypeError):
        XtQuantInstrumentSafety("EQUITY", SecurityStatus.TRADING, Shares(100))  # type: ignore[arg-type]
    with pytest.raises(DomainTypeError):
        XtQuantInstrumentSafety(SecurityType.EQUITY, "TRADING", Shares(100))  # type: ignore[arg-type]
    with pytest.raises(DomainValidationError):
        XtQuantInstrumentSafety(SecurityType.EQUITY, SecurityStatus.TRADING, Shares(0))
    with pytest.raises(DomainTypeError):
        XtQuantFeeBreakdown(Money(Decimal(0)), Money(Decimal(0)), "0")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("factory", "exception"),
    [
        (lambda: _int(True, label="value"), BrokerSchemaMismatch),
        (lambda: _int("1", label="value"), BrokerSchemaMismatch),
        (lambda: _int(-1, label="value"), BrokerSchemaMismatch),
        (lambda: _int(0, label="value", positive=True), BrokerSchemaMismatch),
        (lambda: _signed_int(True, label="value"), BrokerSchemaMismatch),
        (lambda: _signed_int("1", label="value"), BrokerSchemaMismatch),
        (lambda: _text(1, label="value"), BrokerSchemaMismatch),
        (lambda: _text("", label="value"), BrokerSchemaMismatch),
        (lambda: _text(" bad", label="value"), BrokerSchemaMismatch),
        (lambda: _text("x" * 257, label="value"), BrokerSchemaMismatch),
        (lambda: _text("bad\n", label="value"), BrokerSchemaMismatch),
        (lambda: _field({}, "missing", label="object"), BrokerSchemaMismatch),
        (lambda: _field(object(), "missing", label="object"), BrokerSchemaMismatch),
        (lambda: _mapping([], label="mapping"), BrokerSchemaMismatch),
        (lambda: _mapping({1: "value"}, label="mapping"), BrokerSchemaMismatch),
        (lambda: _decimal(True, label="number", maximum_places=2), BrokerSchemaMismatch),
        (lambda: _decimal("1", label="number", maximum_places=2), BrokerSchemaMismatch),
        (lambda: _decimal(math.inf, label="number", maximum_places=2), BrokerSchemaMismatch),
        (lambda: _decimal(Decimal("NaN"), label="number", maximum_places=2), BrokerSchemaMismatch),
        (lambda: _decimal(-1, label="number", maximum_places=2), BrokerSchemaMismatch),
        (
            lambda: _decimal(0, label="number", maximum_places=2, allow_zero=False),
            BrokerSchemaMismatch,
        ),
        (
            lambda: _decimal(Decimal("1.001"), label="number", maximum_places=2),
            BrokerSchemaMismatch,
        ),
        (lambda: _safe_sequence(None, label="query"), BrokerFactUnavailable),
        (lambda: _safe_sequence("text", label="query"), BrokerSchemaMismatch),
        (lambda: _safe_sequence({"not": "sequence"}, label="query"), BrokerSchemaMismatch),
        (
            lambda: _module_attribute(object(), "missing", module_name="module"),
            BrokerDependencyMissing,
        ),
    ],
)
def test_xtquant_untrusted_sdk_primitive_validation(
    factory: Callable[[], object], exception: type[Exception]
) -> None:
    with pytest.raises(exception):
        factory()


def test_xtquant_numeric_rendering_is_canonical() -> None:
    assert _signed_int(-1, label="value") == -1
    assert _text("canonical", label="value") == "canonical"
    assert _field({"value": 3}, "value", label="mapping") == 3
    assert _mapping({"value": 3}, label="mapping") == {"value": 3}
    assert _decimal_text(1.25, label="value", maximum_places=4) == "1.25"
    assert _decimal_text(1, label="value", maximum_places=4) == "1"
    assert _optional_price_text(0, label="price") is None
    assert _optional_price_text(10.1, label="price") == "10.1"
    assert _safe_sequence([], label="query") == ()


def test_sdk_diagnosis_rejects_schema_incompatible_install() -> None:
    modules = OfficialSdkModules()
    modules.modules["xtquant.xttrader"] = SimpleNamespace()
    diagnosis = diagnose_xtquant_sdk(importer=modules.importer)
    assert diagnosis.available is False
    assert "lacks required symbol" in diagnosis.message
    assert diagnosis.real_order_calls == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"facade": object(), "account_id": "account", "clock": lambda: NOW},
        {"facade": ContractXtQuantSdkFacade(), "account_id": "", "clock": lambda: NOW},
        {"facade": ContractXtQuantSdkFacade(), "account_id": "account", "clock": object()},
    ],
)
def test_xtquant_broker_rejects_invalid_dependency_binding(kwargs: dict[str, object]) -> None:
    with pytest.raises((DomainTypeError, BrokerSchemaMismatch)):
        XtQuantBroker(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"userdata_path": "path", "session_id": 1, "account_id": "account"},
        {"userdata_path": Path("missing"), "session_id": 1, "account_id": "account"},
    ],
)
def test_lazy_sdk_loading_requires_real_directory(kwargs: dict[str, object]) -> None:
    with pytest.raises((DomainTypeError, BrokerDependencyMissing)):
        XtQuantBroker.load_sdk(clock=lambda: NOW, **kwargs)  # type: ignore[arg-type]


def test_clock_and_cleanup_failures_fail_closed() -> None:
    gateway = XtQuantBroker(
        facade=ContractXtQuantSdkFacade(),
        account_id="account-001",
        clock=lambda: datetime(2026, 8, 25),
    )
    with pytest.raises(BrokerSchemaMismatch, match="timezone-aware"):
        gateway.health()

    facade = ContractXtQuantSdkFacade()
    facade.connect_result = -1

    def stop_failure() -> None:
        raise RuntimeError("injected cleanup failure")

    facade.stop = stop_failure  # type: ignore[method-assign]
    gateway = XtQuantBroker(facade=facade, account_id="account-001", clock=lambda: NOW)
    with pytest.raises(BrokerDisconnected, match="cleanup"):
        gateway.connect()


def test_connect_is_idempotent_and_disconnect_only_stops_connected_sdk() -> None:
    gateway, facade = broker()
    gateway.disconnect()
    assert facade.stopped is False
    gateway.connect()
    gateway.connect()
    assert facade.started is True
    gateway.disconnect()
    gateway.disconnect()
    assert facade.stopped is True
    assert repr(gateway) == "<XtQuantBroker account=redacted>"


def test_missing_or_malformed_query_facts_are_rejected() -> None:
    gateway, facade = broker()
    gateway.connect()
    facade.asset = None
    with pytest.raises(BrokerFactUnavailable, match="no fact"):
        gateway.query_account()

    facade.positions = None
    with pytest.raises(BrokerFactUnavailable):
        gateway.query_positions()
    facade.positions = "not-a-sequence"
    with pytest.raises(BrokerSchemaMismatch):
        gateway.query_positions()
    facade.orders = None
    with pytest.raises(BrokerFactUnavailable):
        gateway.query_orders()
    facade.trades = None
    with pytest.raises(BrokerFactUnavailable):
        gateway.query_fills()


@pytest.mark.parametrize(
    ("code_name", "expected"),
    [
        ("order_unreported", BrokerOrderStatus.PENDING_NEW),
        ("order_wait_reporting", BrokerOrderStatus.PENDING_NEW),
        ("order_reported", BrokerOrderStatus.ACKNOWLEDGED),
        ("order_reported_cancel", BrokerOrderStatus.PENDING_CANCEL),
        ("order_partsucc_cancel", BrokerOrderStatus.PENDING_CANCEL),
        ("order_part_cancel", BrokerOrderStatus.CANCELLED),
        ("order_canceled", BrokerOrderStatus.CANCELLED),
        ("order_part_succ", BrokerOrderStatus.PARTIALLY_FILLED),
        ("order_succeeded", BrokerOrderStatus.FILLED),
        ("order_junk", BrokerOrderStatus.REJECTED),
        ("order_unknown", BrokerOrderStatus.UNKNOWN),
    ],
)
def test_all_documented_order_statuses_are_mapped_without_guessing(
    code_name: str, expected: BrokerOrderStatus
) -> None:
    gateway, facade = broker()
    assert gateway._order_status(getattr(facade.constants, code_name)) is expected  # type: ignore[attr-defined]


def test_unknown_side_status_and_price_type_are_rejected() -> None:
    gateway, facade = broker()
    with pytest.raises(BrokerSchemaMismatch, match="buy or sell"):
        gateway._side(999, label="side")  # type: ignore[attr-defined]
    with pytest.raises(BrokerSchemaMismatch, match="unknown order status"):
        gateway._order_status(999)  # type: ignore[attr-defined]

    gateway.connect()
    facade.orders = [replace(SdkObject(), price_type=999)]
    with pytest.raises(BrokerSchemaMismatch, match="protected limit"):
        gateway.query_orders()


def test_instrument_identity_and_authoritative_safety_are_required() -> None:
    gateway, facade = broker()
    gateway.connect()
    symbol = Symbol.parse("600519.SH")
    facade.instruments.clear()
    with pytest.raises(BrokerFactUnavailable, match="unavailable"):
        gateway.query_instrument(symbol)

    facade.instruments[symbol.xtquant] = {
        "ExchangeID": "SZ",
        "InstrumentID": "600519",
    }
    with pytest.raises(BrokerSchemaMismatch, match="contradicts"):
        gateway.query_instrument(symbol)

    facade.instruments[symbol.xtquant] = {
        "ExchangeID": "SH",
        "InstrumentID": "600519",
        "PriceTick": 0.01,
        "DownStopPrice": 9,
        "UpStopPrice": 11,
    }
    facade.instrument_safety = lambda _symbol, _detail: object()  # type: ignore[method-assign]
    with pytest.raises(BrokerSchemaMismatch, match="safety fact"):
        gateway.query_instrument(symbol)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, datetime(1970, 1, 1, 8, tzinfo=UTC).astimezone()),
        ("20260825 09:31:00", None),
        ("20260825093100.000", None),
        ("20260825093100", None),
    ],
)
def test_tick_time_accepts_only_documented_formats(raw: object, expected: datetime | None) -> None:
    parsed = XtQuantBroker._event_time({"time": raw})  # type: ignore[attr-defined]
    assert parsed.tzinfo is not None
    if expected is not None:
        assert int(parsed.timestamp()) == 0


@pytest.mark.parametrize("raw", [True, "not-a-time", object()])
def test_tick_time_rejects_unsupported_values(raw: object) -> None:
    with pytest.raises(BrokerSchemaMismatch):
        XtQuantBroker._event_time({"time": raw})  # type: ignore[attr-defined]


def test_quote_requires_authoritative_market_volume_and_book_facts() -> None:
    gateway, facade = broker()
    gateway.connect()
    symbol = Symbol.parse("600519.SH")
    facade.ticks.clear()
    with pytest.raises(BrokerFactUnavailable, match="quote is unavailable"):
        gateway.query_quote(symbol)

    facade.ticks[symbol.xtquant] = full_tick()
    facade.market_status = lambda: "OPEN"  # type: ignore[method-assign,return-value]
    with pytest.raises(BrokerSchemaMismatch, match="market status"):
        gateway.query_quote(symbol)
    facade.market_status = lambda: MarketSessionStatus.OPEN  # type: ignore[method-assign]
    facade.quote_volume_shares = lambda _symbol, _tick: 100  # type: ignore[method-assign,return-value]
    with pytest.raises(BrokerSchemaMismatch, match="volume fact"):
        gateway.query_quote(symbol)

    with pytest.raises(BrokerSchemaMismatch, match="price sequence"):
        XtQuantBroker._book_price({"bidPrice": "10"}, "bidPrice")  # type: ignore[attr-defined]
    assert XtQuantBroker._book_price({"bidPrice": []}, "bidPrice") is None  # type: ignore[attr-defined]


def test_subscription_identity_and_disconnect_callback_are_safe() -> None:
    gateway, facade = broker()
    first: list[dict[str, object]] = []
    second: list[dict[str, object]] = []
    with pytest.raises(DomainTypeError):
        gateway.subscribe(object())  # type: ignore[arg-type]
    callback = first.append
    gateway.subscribe(callback)
    gateway.subscribe(callback)
    with pytest.raises(DomainValidationError, match="already registered"):
        gateway.subscribe(second.append)

    gateway.connect()
    facade.emit("DISCONNECTED", object())
    assert gateway.health().connected is False
    with pytest.raises(BrokerDisconnected):
        gateway.query_account()


def test_fill_callback_without_sink_is_validated_but_not_applied() -> None:
    gateway, facade = broker()
    gateway.connect()
    facade.emit("FILL", replace(SdkObject(), traded_volume=100, traded_price=10.1))
    assert gateway.health().diagnostic_code == "CONNECTED"

    facade.emit("UNSUPPORTED", SdkObject())
    assert gateway.health().diagnostic_code == "CALLBACK_SCHEMA_INVALID"
