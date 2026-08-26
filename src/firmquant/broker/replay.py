"""Deterministic, strictly validated broker recording replay adapter."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from firmquant.domain.broker_facts import (
    BrokerAccountFact,
    BrokerFillFact,
    BrokerOrderFact,
    BrokerPositionFact,
    InstrumentFact,
    MarketSessionStatus,
    QuoteFact,
)
from firmquant.domain.errors import DomainTypeError, DomainValidationError
from firmquant.domain.values import Symbol

from .gateway import (
    BrokerDisconnected,
    BrokerEventSink,
    BrokerFactUnavailable,
    BrokerHealth,
    BrokerOrderCommand,
    BrokerWriteForbidden,
)
from .normalization import (
    BrokerEventEnvelope,
    canonical_raw_payload_sha256,
    normalize_account,
    normalize_broker_event,
    normalize_fill,
    normalize_instrument,
    normalize_order,
    normalize_position,
    normalize_quote,
)

_SCHEMA = "firmquant.broker-recording.v1"
_MAX_RECORDING_BYTES = 64 * 1024 * 1024
_MAX_LINE_BYTES = 2 * 1024 * 1024


class ReplayFormatError(ValueError):
    """Raised when frozen evidence is incomplete, ambiguous, or non-canonical."""


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ReplayFormatError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_float(value: str) -> object:
    raise ReplayFormatError(f"binary floating-point JSON number is forbidden: {value}")


def _json_record(line: str, *, line_number: int) -> dict[str, object]:
    if len(line.encode("utf-8")) > _MAX_LINE_BYTES:
        raise ReplayFormatError(f"recording line {line_number} exceeds size limit")
    try:
        value = json.loads(
            line,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_float=_reject_float,
            parse_constant=_reject_float,
        )
    except (json.JSONDecodeError, TypeError) as error:
        raise ReplayFormatError(f"invalid JSON on recording line {line_number}") from error
    if not isinstance(value, dict):
        raise ReplayFormatError(f"recording line {line_number} must be an object")
    return value


def _exact_keys(value: Mapping[str, object], *, expected: frozenset[str], label: str) -> None:
    keys = frozenset(value)
    if keys != expected:
        missing = sorted(expected - keys)
        unexpected = sorted(keys - expected)
        raise ReplayFormatError(f"{label} schema mismatch; missing={missing!r}, unexpected={unexpected!r}")


def _mapping(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ReplayFormatError(f"{label} must be an object with text keys")
    return dict(value)


def _mapping_list(value: object, *, label: str) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        raise ReplayFormatError(f"{label} must be an array")
    return tuple(_mapping(item, label=f"{label} item") for item in value)


def _aware(value: object, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise ReplayFormatError(f"{label} must be ISO datetime text")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        raise ReplayFormatError(f"{label} must be ISO-8601 datetime") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise ReplayFormatError(f"{label} must be timezone-aware")
    return result


def _market_status(value: object) -> MarketSessionStatus:
    if not isinstance(value, str):
        raise ReplayFormatError("recording market status must be text")
    try:
        return MarketSessionStatus(value.upper())
    except ValueError as error:
        raise ReplayFormatError(f"unknown recording market status: {value!r}") from error


def _unique_by_symbol[T](values: tuple[T, ...], *, label: str) -> dict[Symbol, T]:
    result: dict[Symbol, T] = {}
    for value in values:
        symbol = getattr(value, "symbol", None)
        if not isinstance(symbol, Symbol):
            raise ReplayFormatError(f"{label} item has no normalized symbol")
        if symbol in result:
            raise ReplayFormatError(f"duplicate {label} symbol: {symbol}")
        result[symbol] = value
    return result


class RecordedReplayBroker:
    """Read-only BrokerGateway replaying frozen callbacks in canonical event order."""

    def __init__(
        self,
        *,
        account: BrokerAccountFact,
        positions: tuple[BrokerPositionFact, ...],
        orders: tuple[BrokerOrderFact, ...],
        fills: tuple[BrokerFillFact, ...],
        instruments: dict[Symbol, InstrumentFact],
        quotes: dict[Symbol, QuoteFact],
        market_status: MarketSessionStatus,
        captured_at: datetime,
        events: tuple[tuple[BrokerEventEnvelope, dict[str, object]], ...],
        state_sha256: str,
    ) -> None:
        self._account = account
        self._positions = positions
        self._orders = orders
        self._fills = fills
        self._instruments = dict(instruments)
        self._quotes = dict(quotes)
        self._market_status = market_status
        self._captured_at = captured_at
        self._events = events
        self._state_sha256 = state_sha256
        self._connected = False
        self._write_attempts: list[tuple[str, str]] = []

    @classmethod
    def from_jsonl(cls, path: Path) -> RecordedReplayBroker:
        if not isinstance(path, Path):
            raise DomainTypeError("recording path must be pathlib.Path")
        try:
            size = path.stat().st_size
        except OSError as error:
            raise ReplayFormatError(f"cannot stat broker recording: {path}") from error
        if size <= 0 or size > _MAX_RECORDING_BYTES:
            raise ReplayFormatError("broker recording size is invalid")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as error:
            raise ReplayFormatError(f"cannot read UTF-8 broker recording: {path}") from error
        if not lines or any(not line for line in lines):
            raise ReplayFormatError("broker recording cannot contain blank lines")
        records = tuple(_json_record(line, line_number=index) for index, line in enumerate(lines, start=1))
        state = records[0]
        _exact_keys(
            state,
            expected=frozenset(
                {
                    "schema",
                    "record_type",
                    "captured_at",
                    "account",
                    "positions",
                    "orders",
                    "fills",
                    "instruments",
                    "quotes",
                    "market_status",
                }
            ),
            label="recording state",
        )
        if state["schema"] != _SCHEMA or state["record_type"] != "STATE":
            raise ReplayFormatError("first recording line must be the v1 STATE record")
        captured_at = _aware(state["captured_at"], label="recording captured_at")
        try:
            account = normalize_account(_mapping(state["account"], label="recording account"))
            positions = tuple(
                normalize_position(item)
                for item in _mapping_list(state["positions"], label="recording positions")
            )
            orders = tuple(
                normalize_order(item, received_at=captured_at)
                for item in _mapping_list(state["orders"], label="recording orders")
            )
            fills = tuple(
                normalize_fill(item, received_at=captured_at)
                for item in _mapping_list(state["fills"], label="recording fills")
            )
            instrument_values = tuple(
                normalize_instrument(item)
                for item in _mapping_list(state["instruments"], label="recording instruments")
            )
            quote_values = tuple(
                normalize_quote(item, received_at=captured_at)
                for item in _mapping_list(state["quotes"], label="recording quotes")
            )
        except (DomainTypeError, DomainValidationError) as error:
            raise ReplayFormatError(f"invalid normalized recording state: {error}") from error
        instruments = _unique_by_symbol(instrument_values, label="instrument")
        quotes = _unique_by_symbol(quote_values, label="quote")
        if len({position.symbol for position in positions}) != len(positions):
            raise ReplayFormatError("duplicate recording position symbol")
        if len({order.broker_order_id for order in orders}) != len(orders):
            raise ReplayFormatError("duplicate recording broker order id")
        if len({fill.broker_fill_id for fill in fills}) != len(fills):
            raise ReplayFormatError("duplicate recording broker fill id")
        events: list[tuple[BrokerEventEnvelope, dict[str, object]]] = []
        identities: dict[str, tuple[str, str]] = {}
        for line_number, record in enumerate(records[1:], start=2):
            _exact_keys(
                record,
                expected=frozenset({"schema", "record_type", "event"}),
                label=f"recording event line {line_number}",
            )
            if record["schema"] != _SCHEMA or record["record_type"] != "EVENT":
                raise ReplayFormatError(f"recording line {line_number} is not a v1 EVENT")
            raw_event = _mapping(record["event"], label=f"event line {line_number}")
            try:
                envelope = normalize_broker_event(raw_event, received_at=captured_at)
            except (DomainTypeError, DomainValidationError) as error:
                raise ReplayFormatError(f"invalid normalized event on line {line_number}: {error}") from error
            identity = (envelope.event_type.value, envelope.raw_payload_sha256)
            previous = identities.setdefault(envelope.broker_event_id, identity)
            if previous != identity:
                raise ReplayFormatError(f"broker event identity collision: {envelope.broker_event_id}")
            events.append((envelope, copy.deepcopy(raw_event)))
        events.sort(
            key=lambda item: (
                item[0].event_time,
                item[0].broker_sequence,
                item[0].broker_event_id,
            )
        )
        canonical_state = copy.deepcopy(state)
        canonical_events = [copy.deepcopy(event) for _, event in events]
        canonical_payload = {
            "state": canonical_state,
            "events": canonical_events,
        }
        state_sha256 = canonical_raw_payload_sha256(canonical_payload)
        return cls(
            account=account,
            positions=positions,
            orders=orders,
            fills=fills,
            instruments=instruments,
            quotes=quotes,
            market_status=_market_status(state["market_status"]),
            captured_at=captured_at,
            events=tuple(events),
            state_sha256=state_sha256,
        )

    @property
    def state_sha256(self) -> str:
        return self._state_sha256

    @property
    def write_attempts(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._write_attempts)

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def health(self) -> BrokerHealth:
        return BrokerHealth(
            connected=self._connected,
            read_healthy=self._connected,
            write_healthy=False,
            observed_at=self._captured_at,
            diagnostic_code="REPLAY_READ_ONLY" if self._connected else "DISCONNECTED",
        )

    def _require_connected(self) -> None:
        if not self._connected:
            raise BrokerDisconnected("recorded replay broker is disconnected")

    def query_account(self) -> BrokerAccountFact:
        self._require_connected()
        return self._account

    def query_positions(self) -> tuple[BrokerPositionFact, ...]:
        self._require_connected()
        return self._positions

    def query_orders(self) -> tuple[BrokerOrderFact, ...]:
        self._require_connected()
        return self._orders

    def query_fills(self) -> tuple[BrokerFillFact, ...]:
        self._require_connected()
        return self._fills

    def query_instrument(self, symbol: Symbol) -> InstrumentFact:
        self._require_connected()
        if not isinstance(symbol, Symbol):
            raise DomainTypeError("replay instrument query symbol must be Symbol")
        try:
            return self._instruments[symbol]
        except KeyError as error:
            raise BrokerFactUnavailable(f"recorded instrument unavailable: {symbol}") from error

    def query_quote(self, symbol: Symbol) -> QuoteFact:
        self._require_connected()
        if not isinstance(symbol, Symbol):
            raise DomainTypeError("replay quote query symbol must be Symbol")
        try:
            return self._quotes[symbol]
        except KeyError as error:
            raise BrokerFactUnavailable(f"recorded quote unavailable: {symbol}") from error

    def query_market_status(self) -> MarketSessionStatus:
        self._require_connected()
        return self._market_status

    def submit_order(self, command: BrokerOrderCommand) -> BrokerOrderFact:
        if not isinstance(command, BrokerOrderCommand):
            raise DomainTypeError("replay submit requires BrokerOrderCommand")
        self._write_attempts.append(("SUBMIT", command.execution_id))
        raise BrokerWriteForbidden("recorded replay broker cannot submit orders")

    def cancel_order(self, broker_order_id: str) -> BrokerOrderFact:
        if not isinstance(broker_order_id, str) or not broker_order_id:
            raise DomainValidationError("replay cancel broker order id must be non-empty text")
        self._write_attempts.append(("CANCEL", broker_order_id))
        raise BrokerWriteForbidden("recorded replay broker cannot cancel orders")

    def subscribe(self, callback_sink: BrokerEventSink) -> None:
        self._require_connected()
        if not callable(callback_sink):
            raise DomainTypeError("replay callback sink must be callable")
        for _, event in self._events:
            callback_sink(copy.deepcopy(event))


__all__ = ("RecordedReplayBroker", "ReplayFormatError")
