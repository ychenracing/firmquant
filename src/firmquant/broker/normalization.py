"""Fail-closed conversion from canonical broker adapter payloads to domain facts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType

from firmquant.domain.broker_facts import (
    AccountType,
    BrokerAccountFact,
    BrokerFillFact,
    BrokerOrderFact,
    BrokerOrderStatus,
    BrokerPositionFact,
    FillStatus,
    InstrumentFact,
    MarketSessionStatus,
    PriceType,
    QuoteFact,
    SecurityStatus,
    SecurityType,
    Side,
)
from firmquant.domain.errors import DomainTypeError, DomainValidationError
from firmquant.domain.values import Money, Price, Shares, Symbol


class BrokerEventType(StrEnum):
    ORDER = "ORDER"
    FILL = "FILL"
    QUOTE = "QUOTE"


type NormalizedBrokerEventFact = BrokerOrderFact | BrokerFillFact | QuoteFact


@dataclass(frozen=True, slots=True)
class BrokerEventEnvelope:
    """Immutable callback evidence queued for the single database writer."""

    broker_event_id: str
    event_type: BrokerEventType
    broker_sequence: int
    session_date: date
    event_time: datetime
    received_at: datetime
    fact: NormalizedBrokerEventFact
    safe_payload: Mapping[str, object]
    raw_payload_sha256: str


def _mapping(raw: object, *, label: str) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        raise DomainTypeError(f"{label} must be a mapping")
    if not all(isinstance(key, str) for key in raw):
        raise DomainTypeError(f"{label} keys must be text")
    return dict(raw)


def _schema(
    raw: object,
    *,
    label: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, object]:
    payload = _mapping(raw, label=label)
    keys = frozenset(payload)
    missing = required - keys
    unexpected = keys - required - optional
    if missing:
        raise DomainValidationError(f"{label} missing fields: {sorted(missing)!r}")
    if unexpected:
        raise DomainValidationError(f"{label} unexpected fields: {sorted(unexpected)!r}")
    return payload


def _text(value: object, *, label: str, maximum: int = 256) -> str:
    if not isinstance(value, str):
        raise DomainTypeError(f"{label} must be text")
    if not value or value != value.strip() or len(value) > maximum:
        raise DomainValidationError(f"{label} must be canonical non-empty text")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise DomainValidationError(f"{label} contains control characters")
    return value


def _optional_text(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label=label)


def _enum[EnumT: StrEnum](enum_type: type[EnumT], value: object, *, label: str) -> EnumT:
    text = _text(value, label=label).upper()
    try:
        return enum_type(text)
    except ValueError as error:
        raise DomainValidationError(f"unknown {label}: {value!r}") from error


def _decimal(value: object, *, label: str) -> Decimal:
    if isinstance(value, (bool, int, float)):
        raise DomainTypeError(f"{label} must be a canonical decimal string or Decimal")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, str):
        text = _text(value, label=label)
        try:
            result = Decimal(text)
        except InvalidOperation as error:
            raise DomainValidationError(f"{label} is not a valid decimal") from error
    else:
        raise DomainTypeError(f"{label} must be a canonical decimal string or Decimal")
    if not result.is_finite():
        raise DomainValidationError(f"{label} must be finite")
    return result


def _price(value: object, *, label: str, optional: bool = False) -> Price | None:
    if value is None and optional:
        return None
    return Price(_decimal(value, label=label))


def _money(value: object, *, label: str) -> Money:
    return Money(_decimal(value, label=label))


def _shares(value: object, *, label: str) -> Shares:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainTypeError(f"{label} must be an integer share quantity")
    return Shares(value)


def _sequence(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainTypeError(f"{label} must be an integer")
    if value < 0:
        raise DomainValidationError(f"{label} must be nonnegative")
    return value


def _date(value: object, *, label: str) -> date:
    if isinstance(value, datetime):
        raise DomainTypeError(f"{label} must be date, not datetime")
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise DomainTypeError(f"{label} must be ISO date text")
    text = _text(value, label=label)
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise DomainValidationError(f"{label} must be ISO-8601 date") from error


def _datetime(value: object, *, label: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        text = _text(value, label=label)
        try:
            result = datetime.fromisoformat(text)
        except ValueError as error:
            raise DomainValidationError(f"{label} must be ISO-8601 datetime") from error
    else:
        raise DomainTypeError(f"{label} must be datetime or ISO datetime text")
    if result.tzinfo is None or result.utcoffset() is None:
        raise DomainValidationError(f"{label} must be timezone-aware")
    return result


def _canonical_json_value(value: object, *, label: str) -> object:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise DomainTypeError(f"{label} mapping keys must be text")
        return {key: _canonical_json_value(item, label=f"{label}.{key}") for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item, label=f"{label}[{index}]") for index, item in enumerate(value)]
    raise DomainTypeError(f"{label} contains unsupported raw type {type(value).__name__}")


def canonical_raw_payload_sha256(raw: Mapping[str, object]) -> str:
    """Hash the exact canonical adapter payload without retaining secret-bearing bytes."""

    payload = _canonical_json_value(_mapping(raw, label="raw broker payload"), label="payload")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_account(raw: Mapping[str, object]) -> BrokerAccountFact:
    payload = _schema(
        raw,
        label="broker account",
        required=frozenset({"account_id_hash", "account_type", "available_cash", "total_assets"}),
    )
    account_id_hash = _text(payload["account_id_hash"], label="account id hash")
    return BrokerAccountFact(
        account_id_hash=account_id_hash,
        account_type=_enum(AccountType, payload["account_type"], label="account type"),
        available_cash=_money(payload["available_cash"], label="available cash"),
        total_assets=_money(payload["total_assets"], label="total assets"),
    )


def normalize_position(raw: Mapping[str, object]) -> BrokerPositionFact:
    payload = _schema(
        raw,
        label="broker position",
        required=frozenset({"symbol", "total_shares", "sellable_shares", "average_cost", "market_value"}),
    )
    average_cost = _price(payload["average_cost"], label="average cost", optional=True)
    return BrokerPositionFact(
        symbol=Symbol.parse(_text(payload["symbol"], label="position symbol")),
        total_shares=_shares(payload["total_shares"], label="total shares"),
        sellable_shares=_shares(payload["sellable_shares"], label="sellable shares"),
        average_cost=average_cost,
        market_value=_money(payload["market_value"], label="position market value"),
    )


def normalize_instrument(raw: Mapping[str, object]) -> InstrumentFact:
    payload = _schema(
        raw,
        label="broker instrument",
        required=frozenset(
            {
                "symbol",
                "security_type",
                "status",
                "trading_unit",
                "price_tick",
                "price_precision",
                "lower_limit",
                "upper_limit",
                "session_date",
                "observed_at",
            }
        ),
    )
    precision = payload["price_precision"]
    if isinstance(precision, bool) or not isinstance(precision, int):
        raise DomainTypeError("price precision must be an integer")
    price_tick = _price(payload["price_tick"], label="price tick")
    if price_tick is None:
        raise AssertionError("required price unexpectedly normalized as null")
    return InstrumentFact(
        symbol=Symbol.parse(_text(payload["symbol"], label="instrument symbol")),
        security_type=_enum(SecurityType, payload["security_type"], label="security type"),
        status=_enum(SecurityStatus, payload["status"], label="security status"),
        trading_unit=_shares(payload["trading_unit"], label="trading unit"),
        price_tick=price_tick,
        price_precision=precision,
        lower_limit=_price(payload["lower_limit"], label="lower limit", optional=True),
        upper_limit=_price(payload["upper_limit"], label="upper limit", optional=True),
        session_date=_date(payload["session_date"], label="instrument session date"),
        observed_at=_datetime(payload["observed_at"], label="instrument observed_at"),
    )


def normalize_quote(raw: Mapping[str, object], *, received_at: datetime) -> QuoteFact:
    payload = _schema(
        raw,
        label="broker quote",
        required=frozenset(
            {
                "symbol",
                "last_price",
                "previous_close",
                "bid_price",
                "ask_price",
                "volume",
                "turnover",
                "lower_limit",
                "upper_limit",
                "market_status",
                "sequence",
                "session_date",
                "event_time",
            }
        ),
    )
    return QuoteFact(
        symbol=Symbol.parse(_text(payload["symbol"], label="quote symbol")),
        last_price=_price(payload["last_price"], label="last price", optional=True),
        previous_close=_price(payload["previous_close"], label="previous close", optional=True),
        bid_price=_price(payload["bid_price"], label="bid price", optional=True),
        ask_price=_price(payload["ask_price"], label="ask price", optional=True),
        volume=_shares(payload["volume"], label="quote volume"),
        turnover=_money(payload["turnover"], label="quote turnover"),
        lower_limit=_price(payload["lower_limit"], label="lower limit", optional=True),
        upper_limit=_price(payload["upper_limit"], label="upper limit", optional=True),
        market_status=_enum(MarketSessionStatus, payload["market_status"], label="market status"),
        sequence=_sequence(payload["sequence"], label="quote sequence"),
        session_date=_date(payload["session_date"], label="quote session date"),
        event_time=_datetime(payload["event_time"], label="quote event_time"),
        received_at=_datetime(received_at, label="quote received_at"),
    )


def normalize_order(
    raw: Mapping[str, object],
    *,
    received_at: datetime,
    raw_payload_sha256: str | None = None,
) -> BrokerOrderFact:
    payload = _schema(
        raw,
        label="broker order",
        required=frozenset(
            {
                "broker_order_id",
                "client_order_id",
                "symbol",
                "side",
                "price_type",
                "status",
                "requested_shares",
                "filled_shares",
                "limit_price",
                "session_date",
                "event_time",
                "event_sequence",
            }
        ),
    )
    limit_price = _price(payload["limit_price"], label="limit price")
    if limit_price is None:
        raise AssertionError("required limit price unexpectedly normalized as null")
    digest = raw_payload_sha256 or canonical_raw_payload_sha256(payload)
    return BrokerOrderFact(
        broker_order_id=_text(payload["broker_order_id"], label="broker order id"),
        client_order_id=_optional_text(payload["client_order_id"], label="client order id"),
        symbol=Symbol.parse(_text(payload["symbol"], label="order symbol")),
        side=_enum(Side, payload["side"], label="order side"),
        price_type=_enum(PriceType, payload["price_type"], label="price type"),
        status=_enum(BrokerOrderStatus, payload["status"], label="order status"),
        requested_shares=_shares(payload["requested_shares"], label="requested shares"),
        filled_shares=_shares(payload["filled_shares"], label="filled shares"),
        limit_price=limit_price,
        session_date=_date(payload["session_date"], label="order session date"),
        event_time=_datetime(payload["event_time"], label="order event_time"),
        received_at=_datetime(received_at, label="order received_at"),
        event_sequence=_sequence(payload["event_sequence"], label="order event sequence"),
        raw_payload_sha256=digest,
    )


def normalize_fill(
    raw: Mapping[str, object],
    *,
    received_at: datetime,
    raw_payload_sha256: str | None = None,
) -> BrokerFillFact:
    payload = _schema(
        raw,
        label="broker fill",
        required=frozenset(
            {
                "broker_fill_id",
                "broker_order_id",
                "symbol",
                "side",
                "status",
                "shares",
                "price",
                "commission",
                "stamp_duty",
                "transfer_fee",
                "session_date",
                "event_time",
                "event_sequence",
            }
        ),
    )
    price = _price(payload["price"], label="fill price")
    if price is None:
        raise AssertionError("required fill price unexpectedly normalized as null")
    digest = raw_payload_sha256 or canonical_raw_payload_sha256(payload)
    return BrokerFillFact(
        broker_fill_id=_text(payload["broker_fill_id"], label="broker fill id"),
        broker_order_id=_text(payload["broker_order_id"], label="broker order id"),
        symbol=Symbol.parse(_text(payload["symbol"], label="fill symbol")),
        side=_enum(Side, payload["side"], label="fill side"),
        status=_enum(FillStatus, payload["status"], label="fill status"),
        shares=_shares(payload["shares"], label="fill shares"),
        price=price,
        commission=_money(payload["commission"], label="commission"),
        stamp_duty=_money(payload["stamp_duty"], label="stamp duty"),
        transfer_fee=_money(payload["transfer_fee"], label="transfer fee"),
        session_date=_date(payload["session_date"], label="fill session date"),
        event_time=_datetime(payload["event_time"], label="fill event_time"),
        received_at=_datetime(received_at, label="fill received_at"),
        event_sequence=_sequence(payload["event_sequence"], label="fill event sequence"),
        raw_payload_sha256=digest,
    )


def _safe_payload(fact: NormalizedBrokerEventFact) -> Mapping[str, object]:
    if isinstance(fact, BrokerOrderFact):
        payload: dict[str, object] = {
            "broker_order_id": fact.broker_order_id,
            "client_order_id": fact.client_order_id,
            "symbol": fact.symbol.canonical,
            "side": fact.side.value,
            "price_type": fact.price_type.value,
            "status": fact.status.value,
            "requested_shares": fact.requested_shares.value,
            "filled_shares": fact.filled_shares.value,
            "limit_price": fact.limit_price.canonical,
        }
    elif isinstance(fact, BrokerFillFact):
        payload = {
            "broker_fill_id": fact.broker_fill_id,
            "broker_order_id": fact.broker_order_id,
            "symbol": fact.symbol.canonical,
            "side": fact.side.value,
            "status": fact.status.value,
            "shares": fact.shares.value,
            "price": fact.price.canonical,
            "commission": fact.commission.canonical,
            "stamp_duty": fact.stamp_duty.canonical,
            "transfer_fee": fact.transfer_fee.canonical,
        }
    else:
        payload = {
            "symbol": fact.symbol.canonical,
            "last_price": None if fact.last_price is None else fact.last_price.canonical,
            "previous_close": (None if fact.previous_close is None else fact.previous_close.canonical),
            "bid_price": None if fact.bid_price is None else fact.bid_price.canonical,
            "ask_price": None if fact.ask_price is None else fact.ask_price.canonical,
            "volume": fact.volume.value,
            "turnover": fact.turnover.canonical,
            "market_status": fact.market_status.value,
        }
    payload.update(
        {
            "session_date": fact.session_date.isoformat(),
            "event_time": fact.event_time.isoformat(),
            "received_at": fact.received_at.isoformat(),
        }
    )
    return MappingProxyType(payload)


def normalize_broker_event(raw: Mapping[str, object], *, received_at: datetime) -> BrokerEventEnvelope:
    wrapper = _schema(
        raw,
        label="broker callback",
        required=frozenset({"event_id", "event_type", "payload"}),
    )
    broker_event_id = _text(wrapper["event_id"], label="broker event id")
    event_type = _enum(BrokerEventType, wrapper["event_type"], label="broker event type")
    payload = _mapping(wrapper["payload"], label="broker event payload")
    received = _datetime(received_at, label="broker event received_at")
    digest = canonical_raw_payload_sha256(payload)
    if event_type is BrokerEventType.ORDER:
        order = normalize_order(payload, received_at=received, raw_payload_sha256=digest)
        fact: NormalizedBrokerEventFact = order
        sequence = order.event_sequence
    elif event_type is BrokerEventType.FILL:
        fill = normalize_fill(payload, received_at=received, raw_payload_sha256=digest)
        fact = fill
        sequence = fill.event_sequence
    else:
        quote = normalize_quote(payload, received_at=received)
        fact = quote
        sequence = quote.sequence
    return BrokerEventEnvelope(
        broker_event_id=broker_event_id,
        event_type=event_type,
        broker_sequence=sequence,
        session_date=fact.session_date,
        event_time=fact.event_time,
        received_at=received,
        fact=fact,
        safe_payload=_safe_payload(fact),
        raw_payload_sha256=digest,
    )


__all__ = (
    "BrokerEventEnvelope",
    "BrokerEventType",
    "NormalizedBrokerEventFact",
    "canonical_raw_payload_sha256",
    "normalize_account",
    "normalize_broker_event",
    "normalize_fill",
    "normalize_instrument",
    "normalize_order",
    "normalize_position",
    "normalize_quote",
)
