"""Production-hardened XtQuant broker semantics over the reviewed SDK seam.

This subclass fixes return-field semantics that differ from submit parameters, derives
stable broker event time/identity from broker facts, preserves MiniQMT ``order_remark``
as the recoverable client tag, and forwards operational error/disconnect callbacks.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType

from firmquant.domain.broker_facts import (
    BrokerFillFact,
    BrokerOrderFact,
    BrokerOrderStatus,
    FillStatus,
    PriceType,
    Side,
)
from firmquant.execution.write_outcome import BrokerWriteNotAccepted, BrokerWriteOutcomeUnknown

from .client_identity import client_order_tag, is_client_order_tag
from .gateway import (
    BrokerOrderCommand,
    BrokerWriteForbidden,
    _broker_write_is_authorized,
)
from .normalization import canonical_raw_payload_sha256, normalize_fill, normalize_order
from .xtquant import (
    _PRICE_PLACES,
    _SHANGHAI,
    BrokerSchemaMismatch,
    XtQuantBroker,
    XtQuantFeeBreakdown,
    _decimal_text,
    _field,
    _int,
    _safe_sequence,
    _signed_int,
    _text,
)

_ORDER_STATUS_RANK = {
    BrokerOrderStatus.UNKNOWN: 1,
    BrokerOrderStatus.PENDING_NEW: 10,
    BrokerOrderStatus.ACKNOWLEDGED: 20,
    BrokerOrderStatus.PARTIALLY_FILLED: 30,
    BrokerOrderStatus.PENDING_CANCEL: 40,
    BrokerOrderStatus.CANCELLED: 50,
    BrokerOrderStatus.REJECTED: 50,
    BrokerOrderStatus.EXPIRED: 50,
    BrokerOrderStatus.FILLED: 60,
}


def _optional_field(raw: object, name: str) -> object | None:
    if isinstance(raw, Mapping):
        return raw.get(name)
    return getattr(raw, name, None)


def _broker_time(value: object, *, observed_at: datetime, label: str) -> datetime:
    """Normalize XtQuant HHMMSS/epoch broker timestamps into Asia/Shanghai time."""

    local_date = observed_at.astimezone(_SHANGHAI).date()
    if isinstance(value, bool):
        raise BrokerSchemaMismatch(f"{label} is invalid")
    if isinstance(value, int):
        if value < 0:
            raise BrokerSchemaMismatch(f"{label} must be nonnegative")
        if value >= 10_000_000_000_000:
            text = str(value)
            try:
                return datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=_SHANGHAI)
            except ValueError as error:
                raise BrokerSchemaMismatch(f"{label} calendar timestamp is invalid") from error
        if value >= 1_000_000_000_000:
            try:
                return datetime.fromtimestamp(value / 1_000, tz=_SHANGHAI)
            except (OverflowError, OSError, ValueError) as error:
                raise BrokerSchemaMismatch(f"{label} millisecond epoch is invalid") from error
        if value >= 1_000_000_000:
            try:
                return datetime.fromtimestamp(value, tz=_SHANGHAI)
            except (OverflowError, OSError, ValueError) as error:
                raise BrokerSchemaMismatch(f"{label} epoch is invalid") from error
        text = f"{value:06d}"
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise BrokerSchemaMismatch(f"{label} is empty")
        if text.isdecimal() and len(text) <= 6:
            text = text.zfill(6)
        elif text.isdecimal() and len(text) == 14:
            try:
                return datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=_SHANGHAI)
            except ValueError as error:
                raise BrokerSchemaMismatch(f"{label} calendar timestamp is invalid") from error
        else:
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError as error:
                raise BrokerSchemaMismatch(f"{label} format is unsupported") from error
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                return parsed.replace(tzinfo=_SHANGHAI)
            return parsed.astimezone(_SHANGHAI)
    else:
        raise BrokerSchemaMismatch(f"{label} must be integer or text")

    try:
        hour = int(text[:2])
        minute = int(text[2:4])
        second = int(text[4:6])
        return datetime(
            local_date.year,
            local_date.month,
            local_date.day,
            hour,
            minute,
            second,
            tzinfo=_SHANGHAI,
        )
    except (ValueError, IndexError) as error:
        raise BrokerSchemaMismatch(f"{label} HHMMSS value is invalid") from error


def _order_sequence(status: BrokerOrderStatus, filled_shares: int) -> int:
    return filled_shares * 100 + _ORDER_STATUS_RANK[status]


def _fill_sequence(event_time: datetime) -> int:
    return int(event_time.timestamp() * 1_000_000)


def _client_tag(raw: object) -> str | None:
    value = _optional_field(raw, "order_remark")
    return value if isinstance(value, str) and is_client_order_tag(value) else None


class ProductionXtQuantBroker(XtQuantBroker):
    """XtQuant gateway whose returned facts are stable across query/restart cycles."""

    def __repr__(self) -> str:
        return "<ProductionXtQuantBroker account=redacted>"

    def _order_payload(self, raw: object, *, observed_at: datetime) -> dict[str, object]:
        self._validate_identity(raw, label="XtQuant order")
        # Returned XtOrder.price_type uses a different enumeration from the submit
        # argument. Validate its type only; system ownership is proved by the
        # deterministic client tag and the only submit path still uses FIX_PRICE.
        _int(
            _field(raw, "price_type", label="XtQuant order"),
            label="XtQuant returned order price type",
        )
        status = self._order_status(_field(raw, "order_status", label="XtQuant order"))
        requested_shares = _int(
            _field(raw, "order_volume", label="XtQuant order"),
            label="XtQuant requested volume",
            positive=True,
        )
        filled_shares = _int(
            _field(raw, "traded_volume", label="XtQuant order"),
            label="XtQuant filled volume",
        )
        if filled_shares > requested_shares:
            raise BrokerSchemaMismatch("XtQuant filled volume exceeds requested volume")
        event_time = _broker_time(
            _field(raw, "order_time", label="XtQuant order"),
            observed_at=observed_at,
            label="XtQuant order time",
        )
        return {
            "broker_order_id": str(
                _int(
                    _field(raw, "order_id", label="XtQuant order"),
                    label="XtQuant order id",
                    positive=True,
                )
            ),
            "client_order_id": _client_tag(raw),
            "symbol": _text(
                _field(raw, "stock_code", label="XtQuant order"),
                label="XtQuant order symbol",
            ),
            "side": self._side(
                _field(raw, "order_type", label="XtQuant order"),
                label="XtQuant order type",
            ).value,
            "price_type": PriceType.LIMIT.value,
            "status": status.value,
            "requested_shares": requested_shares,
            "filled_shares": filled_shares,
            "limit_price": _decimal_text(
                _field(raw, "price", label="XtQuant order"),
                label="XtQuant order price",
                maximum_places=_PRICE_PLACES,
                allow_zero=False,
            ),
            "session_date": event_time.date().isoformat(),
            "event_time": event_time.isoformat(),
            "event_sequence": _order_sequence(status, filled_shares),
        }

    def _order(self, raw: object, *, observed_at: datetime) -> BrokerOrderFact:
        return normalize_order(self._order_payload(raw, observed_at=observed_at), received_at=observed_at)

    def query_orders(self) -> tuple[BrokerOrderFact, ...]:
        self._require_connected()
        observed_at = self._now()
        raw_values = _safe_sequence(self._facade.query_stock_orders(), label="XtQuant orders query")
        values = tuple(self._order(raw, observed_at=observed_at) for raw in raw_values)
        return tuple(sorted(values, key=lambda value: value.broker_order_id))

    def _fill_payload(self, raw: object, *, observed_at: datetime) -> dict[str, object]:
        self._validate_identity(raw, label="XtQuant fill")
        fees = self._facade.fill_fees(raw)
        if not isinstance(fees, XtQuantFeeBreakdown):
            raise BrokerSchemaMismatch("XtQuant fee provider returned an invalid fact")
        event_time = _broker_time(
            _field(raw, "traded_time", label="XtQuant fill"),
            observed_at=observed_at,
            label="XtQuant trade time",
        )
        return {
            "broker_fill_id": _text(
                _field(raw, "traded_id", label="XtQuant fill"),
                label="XtQuant fill id",
            ),
            "broker_order_id": str(
                _int(
                    _field(raw, "order_id", label="XtQuant fill"),
                    label="XtQuant fill order id",
                    positive=True,
                )
            ),
            "symbol": _text(
                _field(raw, "stock_code", label="XtQuant fill"),
                label="XtQuant fill symbol",
            ),
            "side": self._side(
                _field(raw, "order_type", label="XtQuant fill"),
                label="XtQuant fill order type",
            ).value,
            "status": FillStatus.CONFIRMED.value,
            "shares": _int(
                _field(raw, "traded_volume", label="XtQuant fill"),
                label="XtQuant fill volume",
                positive=True,
            ),
            "price": _decimal_text(
                _field(raw, "traded_price", label="XtQuant fill"),
                label="XtQuant fill price",
                maximum_places=_PRICE_PLACES,
                allow_zero=False,
            ),
            "commission": fees.commission.canonical,
            "stamp_duty": fees.stamp_duty.canonical,
            "transfer_fee": fees.transfer_fee.canonical,
            "session_date": event_time.date().isoformat(),
            "event_time": event_time.isoformat(),
            "event_sequence": _fill_sequence(event_time),
        }

    def _fill(self, raw: object, *, observed_at: datetime) -> BrokerFillFact:
        return normalize_fill(self._fill_payload(raw, observed_at=observed_at), received_at=observed_at)

    def query_fills(self) -> tuple[BrokerFillFact, ...]:
        self._require_connected()
        observed_at = self._now()
        raw_values = _safe_sequence(self._facade.query_stock_trades(), label="XtQuant fills query")
        values = tuple(self._fill(raw, observed_at=observed_at) for raw in raw_values)
        return tuple(sorted(values, key=lambda value: (value.event_sequence, value.broker_fill_id)))

    def submit_order(self, command: BrokerOrderCommand) -> BrokerOrderFact:
        self._require_connected()
        if not _broker_write_is_authorized():
            raise BrokerWriteForbidden("XtQuant submit requires a freshly authorized BrokerWriteCapability")
        if not isinstance(command, BrokerOrderCommand):
            raise TypeError("XtQuant submit requires BrokerOrderCommand")
        constants = self._facade.constants
        order_type = constants.stock_buy if command.side is Side.BUY else constants.stock_sell
        remark = client_order_tag(command.client_order_id)
        try:
            order_id = self._facade.order_stock(
                command.symbol.xtquant,
                order_type,
                command.requested_shares.value,
                constants.fix_price,
                self._sdk_price(command),
                "firmquant",
                remark,
            )
        except Exception as error:
            raise BrokerWriteOutcomeUnknown("XtQuant submit call outcome is unknown") from error
        if isinstance(order_id, bool) or not isinstance(order_id, int):
            raise BrokerWriteOutcomeUnknown("XtQuant submit returned an invalid order identity")
        if order_id <= 0:
            raise BrokerWriteNotAccepted("XtQuant explicitly rejected the submit request")
        try:
            raw = self._facade.query_stock_order(order_id)
        except Exception as error:
            raise BrokerWriteOutcomeUnknown(
                "XtQuant accepted submit but confirmation query failed"
            ) from error
        if raw is None:
            raise BrokerWriteOutcomeUnknown("XtQuant accepted submit but order is not yet queryable")
        try:
            fact = self._order(raw, observed_at=self._now())
        except Exception as error:
            raise BrokerWriteOutcomeUnknown(
                "XtQuant accepted submit but returned order fact is invalid"
            ) from error
        if fact.broker_order_id != str(order_id) or fact.client_order_id != remark:
            raise BrokerWriteOutcomeUnknown("XtQuant accepted submit but returned identity cannot be proven")
        return fact

    def cancel_order(self, broker_order_id: str) -> BrokerOrderFact:
        self._require_connected()
        if not _broker_write_is_authorized():
            raise BrokerWriteForbidden("XtQuant cancel requires a freshly authorized BrokerWriteCapability")
        canonical = _text(broker_order_id, label="XtQuant broker order id", maximum=32)
        if not canonical.isascii() or not canonical.isdecimal() or int(canonical) <= 0:
            raise BrokerSchemaMismatch("XtQuant broker order id must be a positive integer")
        order_id = int(canonical)
        try:
            result = self._facade.cancel_order_stock(order_id)
        except Exception as error:
            raise BrokerWriteOutcomeUnknown("XtQuant cancel call outcome is unknown") from error
        if isinstance(result, bool) or not isinstance(result, int):
            raise BrokerWriteOutcomeUnknown("XtQuant cancel returned an invalid result")
        if result != 0:
            raise BrokerWriteNotAccepted("XtQuant explicitly rejected the cancel request")
        try:
            raw = self._facade.query_stock_order(order_id)
        except Exception as error:
            raise BrokerWriteOutcomeUnknown(
                "XtQuant accepted cancel but confirmation query failed"
            ) from error
        if raw is None:
            raise BrokerWriteOutcomeUnknown("XtQuant accepted cancel but order state is not queryable")
        try:
            return self._order(raw, observed_at=self._now())
        except Exception as error:
            raise BrokerWriteOutcomeUnknown(
                "XtQuant accepted cancel but returned order fact is invalid"
            ) from error

    def _error_identity(self, raw: object, *, label: str) -> None:
        account_id = _text(
            _field(raw, "account_id", label=label),
            label=f"{label} account identity",
            maximum=128,
        )
        if not hmac.compare_digest(account_id, self._account_id):
            raise BrokerSchemaMismatch(f"{label} account identity does not match binding")

    @staticmethod
    def _operational_event_id(event_type: str, identity: Mapping[str, object]) -> str:
        digest = canonical_raw_payload_sha256(identity)
        return f"xtquant-{event_type.casefold()}-{digest}"

    def _operational_error_payload(self, event_type: str, raw: object) -> dict[str, object]:
        self._error_identity(raw, label=f"XtQuant {event_type.casefold()}")
        observed_at = self._now()
        raw_order_id = _signed_int(
            _field(raw, "order_id", label=f"XtQuant {event_type.casefold()}"),
            label=f"XtQuant {event_type.casefold()} order id",
        )
        error_code = _signed_int(
            _field(raw, "error_id", label=f"XtQuant {event_type.casefold()}"),
            label=f"XtQuant {event_type.casefold()} error id",
        )
        error_message = _text(
            _field(raw, "error_msg", label=f"XtQuant {event_type.casefold()}"),
            label=f"XtQuant {event_type.casefold()} error message",
            maximum=4096,
        )
        client_id = _client_tag(raw) if event_type == "ORDER_ERROR" else None
        return {
            "broker_order_id": None if raw_order_id <= 0 else str(raw_order_id),
            "client_order_id": client_id,
            "error_code": error_code,
            "message_sha256": hashlib.sha256(error_message.encode("utf-8")).hexdigest(),
            "session_date": observed_at.astimezone(_SHANGHAI).date().isoformat(),
            "event_time": observed_at.isoformat(),
        }

    def _on_sdk_event(self, event_type: str, raw: object) -> None:
        try:
            canonical_type = _text(event_type, label="XtQuant callback type", maximum=32).upper()
            observed_at = self._now()
            if canonical_type == "DISCONNECTED":
                with self._lock:
                    self._connected = False
                    self._diagnostic = "DISCONNECTED"
                payload: dict[str, object] = {
                    "session_date": observed_at.astimezone(_SHANGHAI).date().isoformat(),
                    "event_time": observed_at.isoformat(),
                }
                identity = dict(payload)
            elif canonical_type == "ORDER":
                payload = self._order_payload(raw, observed_at=observed_at)
                identity = dict(payload)
            elif canonical_type == "FILL":
                payload = self._fill_payload(raw, observed_at=observed_at)
                identity = dict(payload)
            elif canonical_type in {"ORDER_ERROR", "CANCEL_ERROR"}:
                payload = self._operational_error_payload(canonical_type, raw)
                identity = {
                    key: value for key, value in payload.items() if key not in {"event_time", "session_date"}
                }
            else:
                raise BrokerSchemaMismatch("XtQuant callback type is unsupported")
            safe_payload = MappingProxyType(dict(payload))
            event: dict[str, object] = {
                "event_id": (
                    self._event_id(canonical_type, safe_payload)
                    if canonical_type in {"ORDER", "FILL"}
                    else self._operational_event_id(canonical_type, identity)
                ),
                "event_type": canonical_type,
                "payload": dict(safe_payload),
            }
            canonical_raw_payload_sha256(event)
            with self._lock:
                sink = self._sink
        except Exception:
            with self._lock:
                self._diagnostic = "CALLBACK_SCHEMA_INVALID"
            return
        if sink is not None:
            try:
                sink(event)
            except Exception:
                with self._lock:
                    self._diagnostic = "CALLBACK_SINK_FAILED"
                raise


__all__ = ("ProductionXtQuantBroker",)
