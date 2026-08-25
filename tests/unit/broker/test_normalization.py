from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from firmquant.application.event_pump import (
    BrokerEventQueueOverflow,
    DomainEventPump,
)
from firmquant.broker.normalization import (
    BrokerEventType,
    canonical_raw_payload_sha256,
    normalize_account,
    normalize_broker_event,
    normalize_fill,
    normalize_instrument,
    normalize_order,
    normalize_position,
    normalize_quote,
)
from firmquant.domain.broker_facts import (
    AccountType,
    BrokerOrderStatus,
    FillStatus,
    MarketSessionStatus,
    SecurityStatus,
    Side,
)
from firmquant.domain.errors import DomainTypeError, DomainValidationError

NOW = datetime(2026, 8, 25, 9, 31, tzinfo=UTC)
SESSION = date(2026, 8, 25)


def _account_payload() -> dict[str, object]:
    return {
        "account_id_hash": "a" * 64,
        "account_type": "cash",
        "available_cash": "100000.12",
        "total_assets": "125000.12",
    }


def _position_payload() -> dict[str, object]:
    return {
        "symbol": "600519.SH",
        "total_shares": 200,
        "sellable_shares": 100,
        "average_cost": "10.125",
        "market_value": "2400.00",
    }


def _instrument_payload() -> dict[str, object]:
    return {
        "symbol": "600519.SH",
        "security_type": "equity",
        "status": "trading",
        "trading_unit": 100,
        "price_tick": "0.01",
        "price_precision": 2,
        "lower_limit": "9.00",
        "upper_limit": "11.00",
        "session_date": "2026-08-25",
        "observed_at": "2026-08-25T09:30:01+08:00",
    }


def _quote_payload() -> dict[str, object]:
    return {
        "symbol": "sh600519",
        "last_price": "10.10",
        "previous_close": "10.00",
        "bid_price": "10.09",
        "ask_price": "10.10",
        "volume": 100000,
        "turnover": "1010000.00",
        "lower_limit": "9.00",
        "upper_limit": "11.00",
        "market_status": "open",
        "sequence": 8,
        "session_date": "2026-08-25",
        "event_time": "2026-08-25T09:31:00+08:00",
    }


def _order_payload() -> dict[str, object]:
    return {
        "broker_order_id": "broker-order-1",
        "client_order_id": "uquant-order-1",
        "symbol": "600519.SH",
        "side": "buy",
        "price_type": "limit",
        "status": "acknowledged",
        "requested_shares": 100,
        "filled_shares": 0,
        "limit_price": "10.10",
        "session_date": "2026-08-25",
        "event_time": "2026-08-25T09:31:00+08:00",
        "event_sequence": 9,
    }


def _fill_payload() -> dict[str, object]:
    return {
        "broker_fill_id": "broker-fill-1",
        "broker_order_id": "broker-order-1",
        "symbol": "600519.SH",
        "side": "buy",
        "status": "confirmed",
        "shares": 50,
        "price": "10.10",
        "commission": "5.00",
        "stamp_duty": "0",
        "transfer_fee": "0.01",
        "session_date": "2026-08-25",
        "event_time": "2026-08-25T09:31:01+08:00",
        "event_sequence": 10,
    }


def _order_event() -> dict[str, object]:
    return {
        "event_id": "event-order-1",
        "event_type": "order",
        "payload": _order_payload(),
    }


def test_normalizes_all_broker_fact_families_without_binary_float() -> None:
    account = normalize_account(_account_payload())
    position = normalize_position(_position_payload())
    instrument = normalize_instrument(_instrument_payload())
    quote = normalize_quote(_quote_payload(), received_at=NOW)
    order = normalize_order(_order_payload(), received_at=NOW)
    fill = normalize_fill(_fill_payload(), received_at=NOW)

    assert account.account_type is AccountType.CASH
    assert account.available_cash.value == Decimal("100000.12")
    assert position.symbol.canonical == "sh600519"
    assert instrument.status is SecurityStatus.TRADING
    assert quote.market_status is MarketSessionStatus.OPEN
    assert order.status is BrokerOrderStatus.ACKNOWLEDGED
    assert order.side is Side.BUY
    assert fill.status is FillStatus.CONFIRMED
    assert fill.total_fees.value == Decimal("5.01")


@pytest.mark.parametrize(
    ("payload_factory", "field", "unknown"),
    [
        (_account_payload, "account_type", "crypto"),
        (_instrument_payload, "status", "maybe-trading"),
        (_quote_payload, "market_status", "preopen-ish"),
        (_order_payload, "side", "short"),
        (_order_payload, "status", "accepted-probably"),
        (_fill_payload, "status", "provisional"),
    ],
)
def test_unknown_enums_fail_closed(payload_factory: object, field: str, unknown: str) -> None:
    payload = payload_factory()  # type: ignore[operator]
    payload[field] = unknown
    normalizer = {
        "account_type": lambda raw: normalize_account(raw),
        "market_status": lambda raw: normalize_quote(raw, received_at=NOW),
        "side": lambda raw: normalize_order(raw, received_at=NOW),
        "status": (
            (lambda raw: normalize_instrument(raw))
            if "security_type" in payload
            else (
                (lambda raw: normalize_fill(raw, received_at=NOW))
                if "broker_fill_id" in payload
                else (lambda raw: normalize_order(raw, received_at=NOW))
            )
        ),
    }[field]

    with pytest.raises(DomainValidationError, match="unknown"):
        normalizer(payload)


def test_naive_external_event_time_fails_closed() -> None:
    payload = _order_payload()
    payload["event_time"] = "2026-08-25T09:31:00"

    with pytest.raises(DomainValidationError, match="timezone-aware"):
        normalize_order(payload, received_at=NOW)


@pytest.mark.parametrize("unsafe", [10.1, float("nan"), float("inf"), True])
def test_unconstrained_numeric_inputs_are_rejected(unsafe: object) -> None:
    payload = _order_payload()
    payload["limit_price"] = unsafe

    with pytest.raises((DomainTypeError, DomainValidationError)):
        normalize_order(payload, received_at=NOW)


def test_payload_schema_rejects_unknown_sensitive_fields() -> None:
    payload = _account_payload()
    payload["trading_password"] = "must-never-cross-boundary"

    with pytest.raises(DomainValidationError, match="unexpected fields"):
        normalize_account(payload)


def test_raw_payload_digest_is_order_independent_and_bound_to_exact_payload() -> None:
    left = {"symbol": "600519.SH", "shares": 100, "price": "10.10"}
    right = {"price": "10.10", "shares": 100, "symbol": "600519.SH"}

    assert canonical_raw_payload_sha256(left) == canonical_raw_payload_sha256(right)
    assert canonical_raw_payload_sha256(left) != canonical_raw_payload_sha256(
        {**left, "shares": 200}
    )


def test_broker_event_envelope_owns_digest_and_normalized_metadata() -> None:
    envelope = normalize_broker_event(_order_event(), received_at=NOW)

    assert envelope.event_type is BrokerEventType.ORDER
    assert envelope.broker_event_id == "event-order-1"
    assert envelope.broker_sequence == 9
    assert envelope.session_date == SESSION
    assert envelope.raw_payload_sha256 == canonical_raw_payload_sha256(_order_payload())
    assert envelope.fact.raw_payload_sha256 == envelope.raw_payload_sha256


def test_callback_only_enqueues_until_single_writer_dispatches() -> None:
    written: list[str] = []
    pump = DomainEventPump(capacity=2, clock=lambda: NOW)

    pump.sink(_order_event())

    assert written == []
    assert pump.pending_count == 1
    pump.dispatch_one(lambda envelope: written.append(envelope.broker_event_id))
    assert written == ["event-order-1"]
    assert pump.pending_count == 0


def test_bounded_callback_queue_halts_and_retains_overflow_evidence() -> None:
    pump = DomainEventPump(capacity=1, clock=lambda: NOW)
    pump.sink(_order_event())
    overflow = _order_event()
    overflow["event_id"] = "event-order-2"

    with pytest.raises(BrokerEventQueueOverflow, match="halt required"):
        pump.sink(overflow)

    assert pump.halt_required is True
    assert pump.halt_reason == "BROKER_EVENT_QUEUE_OVERFLOW"
    assert pump.overflow_envelope is not None
    assert pump.overflow_envelope.broker_event_id == "event-order-2"
    assert pump.pending_count == 1
