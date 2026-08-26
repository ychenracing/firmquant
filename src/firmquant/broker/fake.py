"""Programmable broker fake for contract, recovery, and fault-injection tests."""

from __future__ import annotations

import copy
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Self

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
    BrokerGatewayError,
    BrokerHealth,
    BrokerOrderCommand,
)
from .normalization import canonical_raw_payload_sha256


class BrokerOperation(StrEnum):
    SUBMIT = "SUBMIT"
    CANCEL = "CANCEL"


class UnscriptedBrokerOperation(BrokerGatewayError):
    """Raised instead of guessing a write result absent an explicit test script."""


class ScriptedOperationMismatch(BrokerGatewayError):
    """Raised when the observed operation differs from the next scripted operation."""


@dataclass(frozen=True, slots=True)
class ScriptedOutcome:
    """One explicit write result, including accepted-but-response-lost scenarios."""

    operation: BrokerOperation
    response: BrokerOrderFact | None = None
    error: Exception | None = None
    callbacks: tuple[Mapping[str, object], ...] = ()
    connected_after: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.operation, BrokerOperation):
            raise DomainTypeError("scripted operation must be BrokerOperation")
        if self.response is not None and not isinstance(self.response, BrokerOrderFact):
            raise DomainTypeError("scripted response must be BrokerOrderFact or null")
        if self.error is not None and not isinstance(self.error, Exception):
            raise DomainTypeError("scripted error must be Exception or null")
        if self.response is None and self.error is None:
            raise DomainValidationError("scripted outcome must have a response or error")
        if not isinstance(self.callbacks, tuple):
            raise DomainTypeError("scripted callbacks must be a tuple")
        copied: list[Mapping[str, object]] = []
        for callback in self.callbacks:
            if not isinstance(callback, Mapping):
                raise DomainTypeError("scripted callback must be a mapping")
            callback_copy = copy.deepcopy(dict(callback))
            canonical_raw_payload_sha256(callback_copy)
            copied.append(callback_copy)
        object.__setattr__(self, "callbacks", tuple(copied))
        if self.connected_after is not None and not isinstance(self.connected_after, bool):
            raise DomainTypeError("scripted connected_after must be bool or null")


def _typed_tuple[T](values: tuple[T, ...], expected: type[T], *, label: str) -> tuple[T, ...]:
    if not isinstance(values, tuple) or not all(isinstance(value, expected) for value in values):
        raise DomainTypeError(f"fake broker {label} must be a typed tuple")
    return values


class FakeBroker:
    """In-memory BrokerGateway whose every write outcome must be predeclared."""

    def __init__(
        self,
        *,
        account: BrokerAccountFact,
        positions: tuple[BrokerPositionFact, ...],
        orders: tuple[BrokerOrderFact, ...],
        fills: tuple[BrokerFillFact, ...],
        instruments: tuple[InstrumentFact, ...],
        quotes: tuple[QuoteFact, ...],
        market_status: MarketSessionStatus,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(account, BrokerAccountFact):
            raise DomainTypeError("fake broker account must be BrokerAccountFact")
        if not isinstance(market_status, MarketSessionStatus):
            raise DomainTypeError("fake broker market status must be MarketSessionStatus")
        if not callable(clock):
            raise DomainTypeError("fake broker clock must be callable")
        self._account = account
        self._positions = _typed_tuple(positions, BrokerPositionFact, label="positions")
        self._orders = list(_typed_tuple(orders, BrokerOrderFact, label="orders"))
        self._fills = _typed_tuple(fills, BrokerFillFact, label="fills")
        instrument_values = _typed_tuple(instruments, InstrumentFact, label="instruments")
        quote_values = _typed_tuple(quotes, QuoteFact, label="quotes")
        self._instruments = {value.symbol: value for value in instrument_values}
        self._quotes = {value.symbol: value for value in quote_values}
        if len(self._instruments) != len(instrument_values):
            raise DomainValidationError("fake broker contains duplicate instruments")
        if len(self._quotes) != len(quote_values):
            raise DomainValidationError("fake broker contains duplicate quotes")
        if len({order.broker_order_id for order in self._orders}) != len(self._orders):
            raise DomainValidationError("fake broker contains duplicate broker order ids")
        self._market_status = market_status
        self._clock = clock
        self._connected = False
        self._sink: BrokerEventSink | None = None
        self._outcomes: deque[ScriptedOutcome] = deque()
        self._submitted_commands: list[BrokerOrderCommand] = []
        self._cancelled_order_ids: list[str] = []

    @property
    def submitted_commands(self) -> tuple[BrokerOrderCommand, ...]:
        return tuple(self._submitted_commands)

    @property
    def cancelled_order_ids(self) -> tuple[str, ...]:
        return tuple(self._cancelled_order_ids)

    def script(self, outcomes: Iterable[ScriptedOutcome]) -> Self:
        scripted = tuple(outcomes)
        if not all(isinstance(outcome, ScriptedOutcome) for outcome in scripted):
            raise DomainTypeError("fake broker script accepts only ScriptedOutcome")
        self._outcomes.extend(scripted)
        return self

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def health(self) -> BrokerHealth:
        connected = self._connected
        return BrokerHealth(
            connected=connected,
            read_healthy=connected,
            write_healthy=connected,
            observed_at=self._clock(),
            diagnostic_code="CONNECTED" if connected else "DISCONNECTED",
        )

    def _require_connected(self) -> None:
        if not self._connected:
            raise BrokerDisconnected("fake broker is disconnected")

    def query_account(self) -> BrokerAccountFact:
        self._require_connected()
        return self._account

    def query_positions(self) -> tuple[BrokerPositionFact, ...]:
        self._require_connected()
        return self._positions

    def query_orders(self) -> tuple[BrokerOrderFact, ...]:
        self._require_connected()
        return tuple(self._orders)

    def query_fills(self) -> tuple[BrokerFillFact, ...]:
        self._require_connected()
        return self._fills

    def query_instrument(self, symbol: Symbol) -> InstrumentFact:
        self._require_connected()
        if not isinstance(symbol, Symbol):
            raise DomainTypeError("instrument query symbol must be Symbol")
        try:
            return self._instruments[symbol]
        except KeyError as error:
            raise BrokerFactUnavailable(f"instrument unavailable: {symbol}") from error

    def query_quote(self, symbol: Symbol) -> QuoteFact:
        self._require_connected()
        if not isinstance(symbol, Symbol):
            raise DomainTypeError("quote query symbol must be Symbol")
        try:
            return self._quotes[symbol]
        except KeyError as error:
            raise BrokerFactUnavailable(f"quote unavailable: {symbol}") from error

    def query_market_status(self) -> MarketSessionStatus:
        self._require_connected()
        return self._market_status

    def _next_outcome(self, operation: BrokerOperation) -> ScriptedOutcome:
        if not self._outcomes:
            raise UnscriptedBrokerOperation(f"fake broker {operation.value.lower()} has no scripted outcome")
        outcome = self._outcomes.popleft()
        if outcome.operation is not operation:
            raise ScriptedOperationMismatch(f"expected {outcome.operation.value}, observed {operation.value}")
        if outcome.callbacks and self._sink is None:
            raise UnscriptedBrokerOperation("scripted callbacks require subscribe() before broker write")
        return outcome

    def _apply_order(self, response: BrokerOrderFact) -> None:
        for index, current in enumerate(self._orders):
            if current.broker_order_id == response.broker_order_id:
                self._orders[index] = response
                return
        self._orders.append(response)

    def _finish(self, outcome: ScriptedOutcome) -> BrokerOrderFact:
        if outcome.response is not None:
            self._apply_order(outcome.response)
        if self._sink is not None:
            for callback in outcome.callbacks:
                self._sink(copy.deepcopy(dict(callback)))
        if outcome.connected_after is not None:
            self._connected = outcome.connected_after
        if outcome.error is not None:
            raise outcome.error
        if outcome.response is None:
            raise AssertionError("validated scripted outcome lost both response and error")
        return outcome.response

    def submit_order(self, command: BrokerOrderCommand) -> BrokerOrderFact:
        self._require_connected()
        if not isinstance(command, BrokerOrderCommand):
            raise DomainTypeError("fake submit requires BrokerOrderCommand")
        self._submitted_commands.append(command)
        outcome = self._next_outcome(BrokerOperation.SUBMIT)
        if outcome.response is not None and (
            outcome.response.client_order_id != command.client_order_id
            or outcome.response.symbol != command.symbol
            or outcome.response.side != command.side
            or outcome.response.requested_shares != command.requested_shares
        ):
            raise DomainValidationError("scripted submit response contradicts command identity")
        return self._finish(outcome)

    def cancel_order(self, broker_order_id: str) -> BrokerOrderFact:
        self._require_connected()
        if not isinstance(broker_order_id, str) or not broker_order_id:
            raise DomainValidationError("fake cancel broker order id must be non-empty text")
        self._cancelled_order_ids.append(broker_order_id)
        outcome = self._next_outcome(BrokerOperation.CANCEL)
        if outcome.response is not None and outcome.response.broker_order_id != broker_order_id:
            raise DomainValidationError("scripted cancel response contradicts broker order id")
        return self._finish(outcome)

    def subscribe(self, callback_sink: BrokerEventSink) -> None:
        self._require_connected()
        if not callable(callback_sink):
            raise DomainTypeError("fake broker callback sink must be callable")
        self._sink = callback_sink


__all__ = (
    "BrokerOperation",
    "FakeBroker",
    "ScriptedOperationMismatch",
    "ScriptedOutcome",
    "UnscriptedBrokerOperation",
)
