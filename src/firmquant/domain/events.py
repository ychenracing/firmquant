"""Typed order events consumed serially by the durable aggregate."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, fields
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

from .errors import DomainTypeError, DomainValidationError
from .values import Money, Price, Shares, Symbol

_REASON_CODE: Final = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")


def _canonical_text(value: str, *, label: str, maximum: int = 256) -> None:
    if not isinstance(value, str):
        raise DomainTypeError(f"{label} must be text")
    if not value or value != value.strip() or len(value) > maximum:
        raise DomainValidationError(f"{label} must be canonical non-empty text")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise DomainValidationError(f"{label} contains control characters")


def _reason_code(value: str, *, label: str) -> None:
    if not isinstance(value, str) or _REASON_CODE.fullmatch(value) is None:
        raise DomainValidationError(f"{label} must be an uppercase reason code")


def _evidence(value: str, *, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise DomainValidationError(f"{label} must be SHA-256")


@dataclass(frozen=True, slots=True)
class OrderEvent:
    event_id: str

    def __post_init__(self) -> None:
        _canonical_text(self.event_id, label="order event id")


@dataclass(frozen=True, slots=True)
class OrderValidated(OrderEvent):
    pass


@dataclass(frozen=True, slots=True)
class OrderArmed(OrderEvent):
    pass


@dataclass(frozen=True, slots=True)
class SubmitStarted(OrderEvent):
    pass


@dataclass(frozen=True, slots=True)
class SubmitNotAccepted(OrderEvent):
    evidence_sha256: str

    def __post_init__(self) -> None:
        OrderEvent.__post_init__(self)
        _evidence(self.evidence_sha256, label="submit rejection evidence")


@dataclass(frozen=True, slots=True)
class BrokerAcknowledged(OrderEvent):
    broker_order_id: str

    def __post_init__(self) -> None:
        OrderEvent.__post_init__(self)
        _canonical_text(self.broker_order_id, label="broker order id")


@dataclass(frozen=True, slots=True)
class SubmitOutcomeUnknown(OrderEvent):
    diagnostic_code: str

    def __post_init__(self) -> None:
        OrderEvent.__post_init__(self)
        _reason_code(self.diagnostic_code, label="submit diagnostic code")


@dataclass(frozen=True, slots=True)
class UnknownResolvedNotAccepted(OrderEvent):
    evidence_sha256: str

    def __post_init__(self) -> None:
        OrderEvent.__post_init__(self)
        _evidence(self.evidence_sha256, label="unknown resolution evidence")


@dataclass(frozen=True, slots=True)
class FillReported(OrderEvent):
    broker_fill_id: str
    broker_order_id: str
    shares: Shares
    price: Price

    def __post_init__(self) -> None:
        OrderEvent.__post_init__(self)
        _canonical_text(self.broker_fill_id, label="broker fill id")
        _canonical_text(self.broker_order_id, label="broker order id")
        if not isinstance(self.shares, Shares):
            raise DomainTypeError("fill event shares must be Shares")
        if not self.shares.is_positive:
            raise DomainValidationError("fill event shares must be positive")
        if not isinstance(self.price, Price):
            raise DomainTypeError("fill event price must be Price")


@dataclass(frozen=True, slots=True)
class CancelRequested(OrderEvent):
    pass


@dataclass(frozen=True, slots=True)
class CancelNotAccepted(OrderEvent):
    evidence_sha256: str

    def __post_init__(self) -> None:
        OrderEvent.__post_init__(self)
        _evidence(self.evidence_sha256, label="cancel rejection evidence")


@dataclass(frozen=True, slots=True)
class CancelOutcomeUnknown(OrderEvent):
    diagnostic_code: str

    def __post_init__(self) -> None:
        OrderEvent.__post_init__(self)
        _reason_code(self.diagnostic_code, label="cancel diagnostic code")


@dataclass(frozen=True, slots=True)
class CancelConfirmed(OrderEvent):
    broker_order_id: str | None = None

    def __post_init__(self) -> None:
        OrderEvent.__post_init__(self)
        if self.broker_order_id is not None:
            _canonical_text(self.broker_order_id, label="broker order id")


@dataclass(frozen=True, slots=True)
class BrokerRejected(OrderEvent):
    reason_code: str

    def __post_init__(self) -> None:
        OrderEvent.__post_init__(self)
        _reason_code(self.reason_code, label="broker rejection reason")


@dataclass(frozen=True, slots=True)
class OrderExpired(OrderEvent):
    reason_code: str

    def __post_init__(self) -> None:
        OrderEvent.__post_init__(self)
        _reason_code(self.reason_code, label="order expiry reason")


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Symbol):
        return value.canonical
    if isinstance(value, (Money, Price)):
        return value.canonical
    if isinstance(value, Shares):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise DomainTypeError(f"event field has no canonical JSON representation: {type(value).__name__}")


def event_fingerprint(event: OrderEvent) -> str:
    """Hash event type and canonical fields to detect event-id reuse conflicts."""

    if not isinstance(event, OrderEvent):
        raise DomainTypeError("order aggregate accepts only OrderEvent")
    payload = {
        "schema": "firmquant.order-event.v1",
        "type": event.__class__.__name__,
        "fields": {field.name: _json_value(getattr(event, field.name)) for field in fields(event)},
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


type SupportedOrderEvent = (
    OrderValidated
    | OrderArmed
    | SubmitStarted
    | SubmitNotAccepted
    | BrokerAcknowledged
    | SubmitOutcomeUnknown
    | UnknownResolvedNotAccepted
    | FillReported
    | CancelRequested
    | CancelNotAccepted
    | CancelOutcomeUnknown
    | CancelConfirmed
    | BrokerRejected
    | OrderExpired
)


__all__ = (
    "BrokerAcknowledged",
    "BrokerRejected",
    "CancelConfirmed",
    "CancelNotAccepted",
    "CancelOutcomeUnknown",
    "CancelRequested",
    "FillReported",
    "OrderArmed",
    "OrderEvent",
    "OrderExpired",
    "OrderValidated",
    "SubmitNotAccepted",
    "SubmitOutcomeUnknown",
    "SubmitStarted",
    "SupportedOrderEvent",
    "UnknownResolvedNotAccepted",
    "event_fingerprint",
)
