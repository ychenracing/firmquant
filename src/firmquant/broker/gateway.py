"""Strict broker port shared by paper, replay, fake, and live adapters."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol, runtime_checkable

from firmquant.domain.broker_facts import (
    BrokerAccountFact,
    BrokerFillFact,
    BrokerOrderFact,
    BrokerPositionFact,
    InstrumentFact,
    MarketSessionStatus,
    PriceType,
    QuoteFact,
    Side,
)
from firmquant.domain.errors import DomainTypeError, DomainValidationError
from firmquant.domain.values import Price, Shares, Symbol

_EXECUTION_ID = re.compile(r"^exec_[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DIAGNOSTIC = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_WRITE_AUTHORIZED: ContextVar[bool] = ContextVar("firmquant_broker_write_authorized", default=False)


class BrokerGatewayError(RuntimeError):
    """Base error for broker port failures that require explicit handling."""


class BrokerDisconnected(BrokerGatewayError):
    """Raised when an operation requires a healthy broker connection."""


class BrokerFactUnavailable(BrokerGatewayError):
    """Raised when the broker cannot provide a requested authoritative fact."""


class BrokerWriteForbidden(BrokerGatewayError):
    """Raised by read-only gateways before any write side effect."""


@contextmanager
def _broker_write_authorization_scope() -> Iterator[None]:
    """Mark only the dynamically authorized broker call in the current context."""

    token = _WRITE_AUTHORIZED.set(True)
    try:
        yield
    finally:
        _WRITE_AUTHORIZED.reset(token)


def _broker_write_is_authorized() -> bool:
    """Return whether the current call is inside BrokerWriteCapability authorization."""

    return _WRITE_AUTHORIZED.get()


def _canonical_text(value: str, *, label: str, maximum: int = 256) -> None:
    if not isinstance(value, str):
        raise DomainTypeError(f"{label} must be text")
    if not value or value != value.strip() or len(value) > maximum:
        raise DomainValidationError(f"{label} must be canonical non-empty text")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise DomainValidationError(f"{label} contains control characters")


def _aware(value: datetime, *, label: str) -> None:
    if not isinstance(value, datetime):
        raise DomainTypeError(f"{label} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainValidationError(f"{label} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class BrokerHealth:
    """One observed connection-health fact; never an inferred trading permission."""

    connected: bool
    read_healthy: bool
    write_healthy: bool
    observed_at: datetime
    diagnostic_code: str

    def __post_init__(self) -> None:
        for label, value in (
            ("connected", self.connected),
            ("read_healthy", self.read_healthy),
            ("write_healthy", self.write_healthy),
        ):
            if not isinstance(value, bool):
                raise DomainTypeError(f"broker health {label} must be bool")
        _aware(self.observed_at, label="broker health observed_at")
        if not isinstance(self.diagnostic_code, str) or _DIAGNOSTIC.fullmatch(self.diagnostic_code) is None:
            raise DomainValidationError("broker health diagnostic code must be canonical")
        if not self.connected and (self.read_healthy or self.write_healthy):
            raise DomainValidationError("disconnected broker cannot report healthy I/O")
        if self.write_healthy and not self.read_healthy:
            raise DomainValidationError("broker write health requires read health")


@dataclass(frozen=True, slots=True)
class BrokerOrderCommand:
    """Validated broker command carrying a stable economic and execution identity."""

    execution_id: str
    idempotency_key: str
    client_order_id: str
    symbol: Symbol
    side: Side
    price_type: PriceType
    requested_shares: Shares
    limit_price: Price
    strategy_session: date

    def __post_init__(self) -> None:
        if not isinstance(self.execution_id, str) or _EXECUTION_ID.fullmatch(self.execution_id) is None:
            raise DomainValidationError("broker command execution id is invalid")
        if not isinstance(self.idempotency_key, str) or _SHA256.fullmatch(self.idempotency_key) is None:
            raise DomainValidationError("broker command idempotency key must be SHA-256")
        _canonical_text(self.client_order_id, label="broker command client order id")
        if not isinstance(self.symbol, Symbol):
            raise DomainTypeError("broker command symbol must be Symbol")
        if not isinstance(self.side, Side):
            raise DomainTypeError("broker command side must be Side")
        if self.price_type is not PriceType.LIMIT:
            raise DomainValidationError("broker command must use protected LIMIT pricing")
        if not isinstance(self.requested_shares, Shares):
            raise DomainTypeError("broker command requested shares must be Shares")
        if not self.requested_shares.is_positive:
            raise DomainValidationError("broker command requested shares must be positive")
        if not isinstance(self.limit_price, Price):
            raise DomainTypeError("broker command limit price must be Price")
        if isinstance(self.strategy_session, datetime) or not isinstance(self.strategy_session, date):
            raise DomainTypeError("broker command strategy session must be date")


@dataclass(frozen=True, slots=True)
class BrokerOrderAbsenceProof:
    """Authoritative proof that one exact durable submit was never broker-accepted."""

    command: BrokerOrderCommand
    snapshot_id: str
    session_date: date
    captured_at: datetime
    broker_event_watermark: int
    evidence_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.command, BrokerOrderCommand):
            raise DomainTypeError("absence proof command must be BrokerOrderCommand")
        _canonical_text(self.snapshot_id, label="absence proof snapshot id")
        if isinstance(self.session_date, datetime) or not isinstance(self.session_date, date):
            raise DomainTypeError("absence proof session date must be date")
        if self.session_date != self.command.strategy_session:
            raise DomainValidationError("absence proof session differs from durable command")
        _aware(self.captured_at, label="absence proof captured_at")
        if isinstance(self.broker_event_watermark, bool) or not isinstance(self.broker_event_watermark, int):
            raise DomainTypeError("absence proof broker watermark must be integer")
        if self.broker_event_watermark < 0:
            raise DomainValidationError("absence proof broker watermark must be nonnegative")
        if not isinstance(self.evidence_sha256, str) or _SHA256.fullmatch(self.evidence_sha256) is None:
            raise DomainValidationError("absence proof evidence must be SHA-256")


@runtime_checkable
class BrokerEventSink(Protocol):
    """Thread-safe callback target; implementations may only validate and enqueue."""

    def __call__(self, untrusted_event: Mapping[str, object]) -> None: ...


@runtime_checkable
class BrokerGateway(Protocol):
    """Broker capability boundary. External adapters must return normalized facts."""

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def health(self) -> BrokerHealth: ...

    def query_account(self) -> BrokerAccountFact: ...

    def query_positions(self) -> tuple[BrokerPositionFact, ...]: ...

    def query_orders(self) -> tuple[BrokerOrderFact, ...]: ...

    def query_fills(self) -> tuple[BrokerFillFact, ...]: ...

    def query_instrument(self, symbol: Symbol) -> InstrumentFact: ...

    def query_quote(self, symbol: Symbol) -> QuoteFact: ...

    def query_market_status(self) -> MarketSessionStatus: ...

    def submit_order(self, command: BrokerOrderCommand) -> BrokerOrderFact: ...

    def cancel_order(self, broker_order_id: str) -> BrokerOrderFact: ...

    def subscribe(self, callback_sink: BrokerEventSink) -> None: ...


@runtime_checkable
class BrokerOrderAbsenceVerifier(Protocol):
    """Optional read capability; an empty ordinary order query is never sufficient proof."""

    def prove_order_not_accepted(self, command: BrokerOrderCommand) -> BrokerOrderAbsenceProof | None: ...


__all__ = (
    "BrokerDisconnected",
    "BrokerEventSink",
    "BrokerFactUnavailable",
    "BrokerGateway",
    "BrokerGatewayError",
    "BrokerHealth",
    "BrokerOrderAbsenceProof",
    "BrokerOrderAbsenceVerifier",
    "BrokerOrderCommand",
    "BrokerWriteForbidden",
)
