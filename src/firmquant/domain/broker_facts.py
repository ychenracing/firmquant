"""Immutable normalized facts accepted from an untrusted broker boundary."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Final

from .errors import DomainTypeError, DomainValidationError
from .values import Market, Money, Price, Shares, Symbol

_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")


class AccountType(StrEnum):
    CASH = "CASH"
    MARGIN = "MARGIN"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class PriceType(StrEnum):
    LIMIT = "LIMIT"


class BrokerOrderStatus(StrEnum):
    PENDING_NEW = "PENDING_NEW"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class FillStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    REVERSED = "REVERSED"
    UNKNOWN = "UNKNOWN"


class SecurityType(StrEnum):
    EQUITY = "EQUITY"
    FUND = "FUND"
    INDEX = "INDEX"
    UNKNOWN = "UNKNOWN"


class SecurityStatus(StrEnum):
    TRADING = "TRADING"
    SUSPENDED = "SUSPENDED"
    RISK_WARNING = "RISK_WARNING"
    DELISTING = "DELISTING"
    UNKNOWN = "UNKNOWN"


class MarketSessionStatus(StrEnum):
    OPEN = "OPEN"
    AUCTION = "AUCTION"
    BREAK = "BREAK"
    CLOSED = "CLOSED"
    HALTED = "HALTED"
    UNKNOWN = "UNKNOWN"


def _require_aware(value: datetime, *, label: str) -> None:
    if not isinstance(value, datetime):
        raise DomainTypeError(f"{label} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainValidationError(f"{label} must be timezone-aware")


def _require_date(value: date, *, label: str) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise DomainTypeError(f"{label} must be a date")


def _require_nonempty(value: str, *, label: str, maximum: int = 256) -> None:
    if not isinstance(value, str):
        raise DomainTypeError(f"{label} must be text")
    if not value or value != value.strip() or len(value) > maximum:
        raise DomainValidationError(f"{label} must be non-empty canonical text")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise DomainValidationError(f"{label} contains control characters")


def _require_sha256(value: str, *, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise DomainValidationError(f"{label} must be lowercase SHA-256")


def _require_sequence(value: int, *, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainTypeError(f"{label} must be an integer")
    if value < 0:
        raise DomainValidationError(f"{label} must be nonnegative")


@dataclass(frozen=True, slots=True)
class BrokerAccountFact:
    account_id_hash: str
    account_type: AccountType
    available_cash: Money
    total_assets: Money

    def __post_init__(self) -> None:
        _require_sha256(self.account_id_hash, label="account id hash")
        if not isinstance(self.account_type, AccountType):
            raise DomainTypeError("account type must be AccountType")
        if self.available_cash.value > self.total_assets.value:
            raise DomainValidationError("available cash cannot exceed total assets")


@dataclass(frozen=True, slots=True)
class BrokerPositionFact:
    symbol: Symbol
    total_shares: Shares
    sellable_shares: Shares
    average_cost: Price | None
    market_value: Money

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, Symbol):
            raise DomainTypeError("position symbol must be Symbol")
        if self.sellable_shares.value > self.total_shares.value:
            raise DomainValidationError("sellable shares cannot exceed total shares")


@dataclass(frozen=True, slots=True)
class InstrumentFact:
    symbol: Symbol
    security_type: SecurityType
    status: SecurityStatus
    trading_unit: Shares
    price_tick: Price
    price_precision: int
    lower_limit: Price | None
    upper_limit: Price | None
    session_date: date
    observed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, Symbol):
            raise DomainTypeError("instrument symbol must be Symbol")
        if not isinstance(self.security_type, SecurityType):
            raise DomainTypeError("security type must be SecurityType")
        if not isinstance(self.status, SecurityStatus):
            raise DomainTypeError("security status must be SecurityStatus")
        if not self.trading_unit.is_positive:
            raise DomainValidationError("instrument trading unit must be positive")
        if isinstance(self.price_precision, bool) or not isinstance(self.price_precision, int):
            raise DomainTypeError("price precision must be an integer")
        if not 0 <= self.price_precision <= 8:
            raise DomainValidationError("price precision must be between zero and eight")
        prices = tuple(
            price
            for price in (self.price_tick, self.lower_limit, self.upper_limit)
            if price is not None
        )
        if any(price.decimal_places > self.price_precision for price in prices):
            raise DomainValidationError("instrument price exceeds declared precision")
        if (
            self.lower_limit is not None
            and self.upper_limit is not None
            and self.lower_limit.value >= self.upper_limit.value
        ):
            raise DomainValidationError("instrument lower limit must be below upper limit")
        _require_date(self.session_date, label="instrument session date")
        _require_aware(self.observed_at, label="instrument observed_at")

    @property
    def market(self) -> Market:
        return self.symbol.market


@dataclass(frozen=True, slots=True)
class QuoteFact:
    symbol: Symbol
    last_price: Price | None
    previous_close: Price | None
    bid_price: Price | None
    ask_price: Price | None
    volume: Shares
    turnover: Money
    lower_limit: Price | None
    upper_limit: Price | None
    market_status: MarketSessionStatus
    sequence: int
    session_date: date
    event_time: datetime
    received_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, Symbol):
            raise DomainTypeError("quote symbol must be Symbol")
        if not isinstance(self.market_status, MarketSessionStatus):
            raise DomainTypeError("quote market status must be MarketSessionStatus")
        _require_sequence(self.sequence, label="quote sequence")
        _require_date(self.session_date, label="quote session date")
        _require_aware(self.event_time, label="quote event_time")
        _require_aware(self.received_at, label="quote received_at")
        if (
            self.lower_limit is not None
            and self.upper_limit is not None
            and self.lower_limit.value >= self.upper_limit.value
        ):
            raise DomainValidationError("quote lower limit must be below upper limit")
        if (
            self.bid_price is not None
            and self.ask_price is not None
            and self.bid_price.value > self.ask_price.value
        ):
            raise DomainValidationError("quote bid cannot exceed ask")
        bounded_prices = (self.last_price, self.bid_price, self.ask_price)
        for price in bounded_prices:
            if price is None:
                continue
            if self.lower_limit is not None and price.value < self.lower_limit.value:
                raise DomainValidationError("quote price is below broker lower limit")
            if self.upper_limit is not None and price.value > self.upper_limit.value:
                raise DomainValidationError("quote price is above broker upper limit")


@dataclass(frozen=True, slots=True)
class BrokerOrderFact:
    broker_order_id: str
    client_order_id: str | None
    symbol: Symbol
    side: Side
    price_type: PriceType
    status: BrokerOrderStatus
    requested_shares: Shares
    filled_shares: Shares
    limit_price: Price
    session_date: date
    event_time: datetime
    received_at: datetime
    event_sequence: int
    raw_payload_sha256: str

    def __post_init__(self) -> None:
        _require_nonempty(self.broker_order_id, label="broker order id")
        if self.client_order_id is not None:
            _require_nonempty(self.client_order_id, label="client order id")
        if not isinstance(self.symbol, Symbol):
            raise DomainTypeError("order symbol must be Symbol")
        if not isinstance(self.side, Side):
            raise DomainTypeError("order side must be Side")
        if not isinstance(self.price_type, PriceType):
            raise DomainTypeError("order price type must be PriceType")
        if not isinstance(self.status, BrokerOrderStatus):
            raise DomainTypeError("broker order status must be BrokerOrderStatus")
        if not self.requested_shares.is_positive:
            raise DomainValidationError("broker order requested shares must be positive")
        if self.filled_shares.value > self.requested_shares.value:
            raise DomainValidationError("broker order filled shares exceed requested shares")
        _require_date(self.session_date, label="broker order session date")
        _require_aware(self.event_time, label="broker order event_time")
        _require_aware(self.received_at, label="broker order received_at")
        _require_sequence(self.event_sequence, label="broker order event sequence")
        _require_sha256(self.raw_payload_sha256, label="broker order raw payload hash")


@dataclass(frozen=True, slots=True)
class BrokerFillFact:
    broker_fill_id: str
    broker_order_id: str
    symbol: Symbol
    side: Side
    status: FillStatus
    shares: Shares
    price: Price
    commission: Money
    stamp_duty: Money
    transfer_fee: Money
    session_date: date
    event_time: datetime
    received_at: datetime
    event_sequence: int
    raw_payload_sha256: str

    def __post_init__(self) -> None:
        _require_nonempty(self.broker_fill_id, label="broker fill id")
        _require_nonempty(self.broker_order_id, label="broker order id")
        if not isinstance(self.symbol, Symbol):
            raise DomainTypeError("fill symbol must be Symbol")
        if not isinstance(self.side, Side):
            raise DomainTypeError("fill side must be Side")
        if not isinstance(self.status, FillStatus):
            raise DomainTypeError("fill status must be FillStatus")
        if not self.shares.is_positive:
            raise DomainValidationError("fill shares must be positive")
        _require_date(self.session_date, label="fill session date")
        _require_aware(self.event_time, label="fill event_time")
        _require_aware(self.received_at, label="fill received_at")
        _require_sequence(self.event_sequence, label="fill event sequence")
        _require_sha256(self.raw_payload_sha256, label="fill raw payload hash")

    @property
    def total_fees(self) -> Money:
        return Money(self.commission.value + self.stamp_duty.value + self.transfer_fee.value)


@dataclass(frozen=True, slots=True)
class BrokerSnapshot:
    snapshot_id: str
    account: BrokerAccountFact
    positions: tuple[BrokerPositionFact, ...]
    orders: tuple[BrokerOrderFact, ...]
    fills: tuple[BrokerFillFact, ...]
    session_date: date
    captured_at: datetime
    broker_event_watermark: int
    raw_payload_sha256: str
    complete: bool

    def __post_init__(self) -> None:
        _require_nonempty(self.snapshot_id, label="broker snapshot id")
        if not isinstance(self.account, BrokerAccountFact):
            raise DomainTypeError("broker snapshot account must be BrokerAccountFact")
        for label, values, expected_type in (
            ("positions", self.positions, BrokerPositionFact),
            ("orders", self.orders, BrokerOrderFact),
            ("fills", self.fills, BrokerFillFact),
        ):
            if not isinstance(values, tuple) or not all(
                isinstance(value, expected_type) for value in values
            ):
                raise DomainTypeError(f"broker snapshot {label} must be a typed tuple")
        position_symbols = [position.symbol for position in self.positions]
        if len(position_symbols) != len(set(position_symbols)):
            raise DomainValidationError("broker snapshot contains duplicate position symbols")
        order_ids = [order.broker_order_id for order in self.orders]
        if len(order_ids) != len(set(order_ids)):
            raise DomainValidationError("broker snapshot contains duplicate broker order ids")
        fill_ids = [fill.broker_fill_id for fill in self.fills]
        if len(fill_ids) != len(set(fill_ids)):
            raise DomainValidationError("broker snapshot contains duplicate broker fill ids")
        _require_date(self.session_date, label="broker snapshot session date")
        _require_aware(self.captured_at, label="broker snapshot captured_at")
        _require_sequence(self.broker_event_watermark, label="broker event watermark")
        _require_sha256(self.raw_payload_sha256, label="broker snapshot raw payload hash")
        if self.complete is not True:
            raise DomainValidationError("broker snapshot must be explicitly complete")


__all__ = (
    "AccountType",
    "BrokerAccountFact",
    "BrokerFillFact",
    "BrokerOrderFact",
    "BrokerOrderStatus",
    "BrokerPositionFact",
    "BrokerSnapshot",
    "FillStatus",
    "InstrumentFact",
    "MarketSessionStatus",
    "PriceType",
    "QuoteFact",
    "SecurityStatus",
    "SecurityType",
    "Side",
)
