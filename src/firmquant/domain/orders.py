"""Durable order intent identity and deterministic event-sourced state machine."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from .broker_facts import Side
from .errors import DomainTransitionError, DomainTypeError, DomainValidationError
from .events import (
    BrokerAcknowledged,
    BrokerRejected,
    CancelConfirmed,
    CancelNotAccepted,
    CancelOutcomeUnknown,
    CancelRequested,
    FillReported,
    OrderArmed,
    OrderEvent,
    OrderExpired,
    OrderValidated,
    SubmitNotAccepted,
    SubmitOutcomeUnknown,
    SubmitStarted,
    SupportedOrderEvent,
    UnknownResolvedNotAccepted,
    event_fingerprint,
)
from .values import Price, Shares, Symbol

_GIT_SHA: Final = re.compile(r"^[0-9a-f]{40}$")
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")


class OrderState(StrEnum):
    PLANNED = "PLANNED"
    VALIDATED = "VALIDATED"
    ARMED = "ARMED"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


TERMINAL_ORDER_STATES: Final = frozenset(
    {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED}
)

ORDER_TRANSITIONS: Final = MappingProxyType(
    {
        OrderState.PLANNED: frozenset({OrderState.VALIDATED, OrderState.EXPIRED}),
        OrderState.VALIDATED: frozenset({OrderState.ARMED, OrderState.EXPIRED}),
        OrderState.ARMED: frozenset({OrderState.SUBMITTING, OrderState.EXPIRED}),
        OrderState.SUBMITTING: frozenset(
            {
                OrderState.ARMED,
                OrderState.ACKNOWLEDGED,
                OrderState.PARTIALLY_FILLED,
                OrderState.FILLED,
                OrderState.CANCELLED,
                OrderState.REJECTED,
                OrderState.UNKNOWN,
            }
        ),
        OrderState.ACKNOWLEDGED: frozenset(
            {
                OrderState.PARTIALLY_FILLED,
                OrderState.FILLED,
                OrderState.CANCEL_REQUESTED,
                OrderState.CANCELLED,
                OrderState.REJECTED,
                OrderState.EXPIRED,
                OrderState.UNKNOWN,
            }
        ),
        OrderState.PARTIALLY_FILLED: frozenset(
            {
                OrderState.FILLED,
                OrderState.CANCEL_REQUESTED,
                OrderState.CANCELLED,
                OrderState.REJECTED,
                OrderState.EXPIRED,
                OrderState.UNKNOWN,
            }
        ),
        OrderState.CANCEL_REQUESTED: frozenset(
            {
                OrderState.ACKNOWLEDGED,
                OrderState.PARTIALLY_FILLED,
                OrderState.FILLED,
                OrderState.CANCELLED,
                OrderState.REJECTED,
                OrderState.EXPIRED,
                OrderState.UNKNOWN,
            }
        ),
        OrderState.UNKNOWN: frozenset(
            {
                OrderState.ARMED,
                OrderState.ACKNOWLEDGED,
                OrderState.PARTIALLY_FILLED,
                OrderState.FILLED,
                OrderState.CANCELLED,
                OrderState.REJECTED,
                OrderState.EXPIRED,
            }
        ),
        OrderState.FILLED: frozenset(),
        OrderState.CANCELLED: frozenset(),
        OrderState.REJECTED: frozenset(),
        OrderState.EXPIRED: frozenset(),
    }
)


def _canonical_id(value: str, *, label: str, maximum: int = 256) -> None:
    if not isinstance(value, str):
        raise DomainTypeError(f"{label} must be text")
    if not value or value != value.strip() or len(value) > maximum:
        raise DomainValidationError(f"{label} must be canonical non-empty text")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise DomainValidationError(f"{label} contains control characters")


def _intent_hashes(
    *,
    decision_id: str,
    uquant_order_id: str,
    symbol: Symbol,
    side: Side,
    requested_shares: Shares,
    strategy_session: date,
    uquant_source_sha: str,
) -> tuple[str, str]:
    payload = {
        "schema": "firmquant.execution-intent.v1",
        "decision_id": decision_id,
        "uquant_order_id": uquant_order_id,
        "symbol": symbol.canonical,
        "side": side.value,
        "requested_shares": requested_shares.value,
        "strategy_session": strategy_session.isoformat(),
        "uquant_source_sha": uquant_source_sha,
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    idempotency_key = hashlib.sha256(encoded).hexdigest()
    execution_id = "exec_" + hashlib.sha256(b"firmquant.execution.v1\0" + encoded).hexdigest()
    return execution_id, idempotency_key


@dataclass(frozen=True, slots=True)
class ExecutionIntent:
    """Immutable maximum economic authorization derived from one uquant order intent."""

    execution_id: str
    idempotency_key: str
    decision_id: str
    uquant_order_id: str
    symbol: Symbol
    side: Side
    requested_shares: Shares
    strategy_session: date
    uquant_source_sha: str

    def __post_init__(self) -> None:
        _canonical_id(self.decision_id, label="decision id")
        _canonical_id(self.uquant_order_id, label="uquant order id")
        if not isinstance(self.symbol, Symbol):
            raise DomainTypeError("execution intent symbol must be Symbol")
        if not isinstance(self.side, Side):
            raise DomainTypeError("execution intent side must be Side")
        if not isinstance(self.requested_shares, Shares):
            raise DomainTypeError("execution intent requested shares must be Shares")
        if not self.requested_shares.is_positive:
            raise DomainValidationError("execution intent requested shares must be positive")
        if isinstance(self.strategy_session, datetime) or not isinstance(self.strategy_session, date):
            raise DomainTypeError("strategy session must be a date")
        if not isinstance(self.uquant_source_sha, str) or _GIT_SHA.fullmatch(self.uquant_source_sha) is None:
            raise DomainValidationError("uquant source SHA must be a 40-character Git SHA")
        expected_execution_id, expected_key = _intent_hashes(
            decision_id=self.decision_id,
            uquant_order_id=self.uquant_order_id,
            symbol=self.symbol,
            side=self.side,
            requested_shares=self.requested_shares,
            strategy_session=self.strategy_session,
            uquant_source_sha=self.uquant_source_sha,
        )
        if self.execution_id != expected_execution_id:
            raise DomainValidationError("execution id does not match economic intent")
        if self.idempotency_key != expected_key:
            raise DomainValidationError("idempotency key does not match economic intent")

    @classmethod
    def create(
        cls,
        *,
        decision_id: str,
        uquant_order_id: str,
        symbol: Symbol,
        side: Side,
        requested_shares: Shares,
        strategy_session: date,
        uquant_source_sha: str,
    ) -> ExecutionIntent:
        execution_id, idempotency_key = _intent_hashes(
            decision_id=decision_id,
            uquant_order_id=uquant_order_id,
            symbol=symbol,
            side=side,
            requested_shares=requested_shares,
            strategy_session=strategy_session,
            uquant_source_sha=uquant_source_sha,
        )
        return cls(
            execution_id=execution_id,
            idempotency_key=idempotency_key,
            decision_id=decision_id,
            uquant_order_id=uquant_order_id,
            symbol=symbol,
            side=side,
            requested_shares=requested_shares,
            strategy_session=strategy_session,
            uquant_source_sha=uquant_source_sha,
        )


@dataclass(frozen=True, slots=True)
class AppliedEventIdentity:
    event_id: str
    fingerprint: str

    def __post_init__(self) -> None:
        _canonical_id(self.event_id, label="applied event id")
        if not isinstance(self.fingerprint, str) or _SHA256.fullmatch(self.fingerprint) is None:
            raise DomainValidationError("applied event fingerprint must be SHA-256")


@dataclass(frozen=True, slots=True)
class AppliedFill:
    broker_fill_id: str
    broker_order_id: str
    shares: Shares
    price: Price

    def __post_init__(self) -> None:
        _canonical_id(self.broker_fill_id, label="applied broker fill id")
        _canonical_id(self.broker_order_id, label="applied broker order id")
        if not isinstance(self.shares, Shares) or not self.shares.is_positive:
            raise DomainValidationError("applied fill shares must be positive Shares")
        if not isinstance(self.price, Price):
            raise DomainTypeError("applied fill price must be Price")


@dataclass(frozen=True, slots=True)
class OrderAggregate:
    """Single-writer aggregate that retains fills and rejects unsafe state regression."""

    intent: ExecutionIntent
    state: OrderState
    broker_order_id: str | None
    filled_shares: Shares
    fills: tuple[AppliedFill, ...]
    applied_events: tuple[AppliedEventIdentity, ...]
    submit_attempts: int
    cancel_requests: int
    late_fill_investigation_required: bool
    anomalies: tuple[str, ...]
    version: int

    def __post_init__(self) -> None:
        if not isinstance(self.intent, ExecutionIntent):
            raise DomainTypeError("order aggregate intent must be ExecutionIntent")
        if not isinstance(self.state, OrderState):
            raise DomainTypeError("order aggregate state must be OrderState")
        if self.broker_order_id is not None:
            _canonical_id(self.broker_order_id, label="aggregate broker order id")
        if not isinstance(self.filled_shares, Shares):
            raise DomainTypeError("aggregate filled shares must be Shares")
        if not isinstance(self.fills, tuple) or not all(isinstance(fill, AppliedFill) for fill in self.fills):
            raise DomainTypeError("aggregate fills must be an AppliedFill tuple")
        if sum(fill.shares.value for fill in self.fills) != self.filled_shares.value:
            raise DomainValidationError("aggregate filled shares do not match retained fills")
        fill_ids = [fill.broker_fill_id for fill in self.fills]
        if len(fill_ids) != len(set(fill_ids)):
            raise DomainValidationError("aggregate contains duplicate broker fill ids")
        if not isinstance(self.applied_events, tuple) or not all(
            isinstance(event, AppliedEventIdentity) for event in self.applied_events
        ):
            raise DomainTypeError("aggregate applied events must be a typed tuple")
        event_ids = [event.event_id for event in self.applied_events]
        if len(event_ids) != len(set(event_ids)):
            raise DomainValidationError("aggregate contains duplicate event ids")
        for label, value in (
            ("submit attempts", self.submit_attempts),
            ("cancel requests", self.cancel_requests),
            ("aggregate version", self.version),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise DomainTypeError(f"{label} must be an integer")
            if value < 0:
                raise DomainValidationError(f"{label} must be nonnegative")
        if not isinstance(self.late_fill_investigation_required, bool):
            raise DomainTypeError("late-fill investigation flag must be boolean")
        if not isinstance(self.anomalies, tuple) or any(
            not isinstance(anomaly, str) or not anomaly for anomaly in self.anomalies
        ):
            raise DomainTypeError("aggregate anomalies must be a non-empty string tuple")
        if len(self.anomalies) != len(set(self.anomalies)):
            raise DomainValidationError("aggregate anomalies must be unique")
        if self.filled_shares.value > self.intent.requested_shares.value:
            raise DomainValidationError("aggregate filled shares exceed requested shares")
        if self.state is OrderState.FILLED and (
            self.filled_shares.value != self.intent.requested_shares.value
        ):
            raise DomainValidationError("FILLED order must equal requested shares")
        if self.state is OrderState.PARTIALLY_FILLED and not (
            0 < self.filled_shares.value < self.intent.requested_shares.value
        ):
            raise DomainValidationError("PARTIALLY_FILLED order has invalid cumulative shares")

    @classmethod
    def from_intent(cls, intent: ExecutionIntent) -> OrderAggregate:
        return cls(
            intent=intent,
            state=OrderState.PLANNED,
            broker_order_id=None,
            filled_shares=Shares(0),
            fills=(),
            applied_events=(),
            submit_attempts=0,
            cancel_requests=0,
            late_fill_investigation_required=False,
            anomalies=(),
            version=0,
        )

    def _event_seen(self, event: OrderEvent) -> bool:
        fingerprint = event_fingerprint(event)
        existing = next(
            (item for item in self.applied_events if item.event_id == event.event_id),
            None,
        )
        if existing is None:
            return False
        if existing.fingerprint != fingerprint:
            raise DomainTransitionError(f"order event identity collision: {event.event_id}")
        return True

    def _updated(
        self,
        event: OrderEvent,
        *,
        state: OrderState | None = None,
        broker_order_id: str | None = None,
        filled_shares: Shares | None = None,
        fills: tuple[AppliedFill, ...] | None = None,
        submit_attempts: int | None = None,
        cancel_requests: int | None = None,
        late_fill_investigation_required: bool | None = None,
        anomalies: tuple[str, ...] | None = None,
    ) -> OrderAggregate:
        target = self.state if state is None else state
        if target is not self.state and target not in ORDER_TRANSITIONS[self.state]:
            raise DomainTransitionError(
                f"illegal order transition {self.state.value} via "
                f"{event.__class__.__name__} to {target.value}"
            )
        next_broker_order_id = self.broker_order_id
        if broker_order_id is not None:
            if next_broker_order_id is not None and next_broker_order_id != broker_order_id:
                raise DomainTransitionError("broker order id changed for one execution intent")
            next_broker_order_id = broker_order_id
        identity = AppliedEventIdentity(
            event_id=event.event_id,
            fingerprint=event_fingerprint(event),
        )
        return replace(
            self,
            state=target,
            broker_order_id=next_broker_order_id,
            filled_shares=self.filled_shares if filled_shares is None else filled_shares,
            fills=self.fills if fills is None else fills,
            applied_events=(*self.applied_events, identity),
            submit_attempts=(self.submit_attempts if submit_attempts is None else submit_attempts),
            cancel_requests=self.cancel_requests if cancel_requests is None else cancel_requests,
            late_fill_investigation_required=(
                self.late_fill_investigation_required
                if late_fill_investigation_required is None
                else late_fill_investigation_required
            ),
            anomalies=self.anomalies if anomalies is None else anomalies,
            version=self.version + 1,
        )

    def _with_anomaly(self, anomaly: str) -> tuple[str, ...]:
        return self.anomalies if anomaly in self.anomalies else (*self.anomalies, anomaly)

    def _apply_fill(self, event: FillReported) -> OrderAggregate:
        existing = next(
            (fill for fill in self.fills if fill.broker_fill_id == event.broker_fill_id),
            None,
        )
        candidate = AppliedFill(
            broker_fill_id=event.broker_fill_id,
            broker_order_id=event.broker_order_id,
            shares=event.shares,
            price=event.price,
        )
        if existing is not None:
            if existing == candidate:
                return self
            raise DomainTransitionError(f"broker fill identity collision: {event.broker_fill_id}")
        allowed_states = {
            OrderState.SUBMITTING,
            OrderState.UNKNOWN,
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIALLY_FILLED,
            OrderState.CANCEL_REQUESTED,
            *TERMINAL_ORDER_STATES,
        }
        if self.state not in allowed_states:
            raise DomainTransitionError(f"illegal order transition {self.state.value} via FillReported")
        cumulative = self.filled_shares.value + event.shares.value
        if cumulative > self.intent.requested_shares.value:
            raise DomainTransitionError("cumulative fill shares exceed requested shares")
        next_fills = (*self.fills, candidate)
        next_shares = Shares(cumulative)
        if self.state in {
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        }:
            anomaly = f"LATE_FILL_AFTER_{self.state.value}"
            return self._updated(
                event,
                broker_order_id=event.broker_order_id,
                filled_shares=next_shares,
                fills=next_fills,
                late_fill_investigation_required=True,
                anomalies=self._with_anomaly(anomaly),
            )
        target = (
            OrderState.FILLED
            if cumulative == self.intent.requested_shares.value
            else OrderState.PARTIALLY_FILLED
        )
        if self.state is OrderState.CANCEL_REQUESTED and target is OrderState.PARTIALLY_FILLED:
            target = OrderState.CANCEL_REQUESTED
        return self._updated(
            event,
            state=target,
            broker_order_id=event.broker_order_id,
            filled_shares=next_shares,
            fills=next_fills,
        )

    def apply(self, event: SupportedOrderEvent) -> OrderAggregate:
        """Apply one at-least-once event without dropping late broker reality."""

        if not isinstance(event, OrderEvent):
            raise DomainTypeError("order aggregate accepts only OrderEvent")
        if self._event_seen(event):
            return self
        if isinstance(event, OrderValidated):
            return self._updated(event, state=OrderState.VALIDATED)
        if isinstance(event, OrderArmed):
            return self._updated(event, state=OrderState.ARMED)
        if isinstance(event, SubmitStarted):
            return self._updated(
                event,
                state=OrderState.SUBMITTING,
                submit_attempts=self.submit_attempts + 1,
            )
        if isinstance(event, SubmitNotAccepted):
            if self.state is not OrderState.SUBMITTING:
                raise DomainTransitionError(
                    f"illegal order transition {self.state.value} via SubmitNotAccepted"
                )
            return self._updated(event, state=OrderState.ARMED)
        if isinstance(event, SubmitOutcomeUnknown):
            if self.state in {
                OrderState.SUBMITTING,
                OrderState.ACKNOWLEDGED,
                OrderState.PARTIALLY_FILLED,
                OrderState.CANCEL_REQUESTED,
            }:
                return self._updated(event, state=OrderState.UNKNOWN)
            if self.state in {
                OrderState.UNKNOWN,
                OrderState.FILLED,
                OrderState.CANCELLED,
                OrderState.REJECTED,
                OrderState.EXPIRED,
            }:
                return self._updated(event)
            return self._updated(event, state=OrderState.UNKNOWN)
        if isinstance(event, UnknownResolvedNotAccepted):
            return self._updated(event, state=OrderState.ARMED)
        if isinstance(event, BrokerAcknowledged):
            if self.state in {OrderState.SUBMITTING, OrderState.UNKNOWN}:
                return self._updated(
                    event,
                    state=OrderState.ACKNOWLEDGED,
                    broker_order_id=event.broker_order_id,
                )
            if self.state in {
                OrderState.ACKNOWLEDGED,
                OrderState.PARTIALLY_FILLED,
                OrderState.FILLED,
                OrderState.CANCEL_REQUESTED,
                OrderState.CANCELLED,
            }:
                return self._updated(event, broker_order_id=event.broker_order_id)
            if self.state in {OrderState.REJECTED, OrderState.EXPIRED}:
                return self._updated(
                    event,
                    broker_order_id=event.broker_order_id,
                    anomalies=self._with_anomaly(f"ACK_AFTER_{self.state.value}"),
                )
            return self._updated(event, state=OrderState.ACKNOWLEDGED)
        if isinstance(event, FillReported):
            return self._apply_fill(event)
        if isinstance(event, CancelRequested):
            if self.broker_order_id is None:
                raise DomainTransitionError("cannot request cancel without broker order id")
            return self._updated(
                event,
                state=OrderState.CANCEL_REQUESTED,
                cancel_requests=self.cancel_requests + 1,
            )
        if isinstance(event, CancelNotAccepted):
            if self.state is not OrderState.CANCEL_REQUESTED:
                raise DomainTransitionError(
                    f"illegal order transition {self.state.value} via CancelNotAccepted"
                )
            target = (
                OrderState.PARTIALLY_FILLED if self.filled_shares.is_positive else OrderState.ACKNOWLEDGED
            )
            return self._updated(event, state=target)
        if isinstance(event, CancelOutcomeUnknown):
            if self.state is not OrderState.CANCEL_REQUESTED:
                raise DomainTransitionError(
                    f"illegal order transition {self.state.value} via CancelOutcomeUnknown"
                )
            return self._updated(event, state=OrderState.UNKNOWN)
        if isinstance(event, CancelConfirmed):
            confirmed_id = event.broker_order_id or self.broker_order_id
            if confirmed_id is None:
                raise DomainTransitionError("cancel confirmation lacks broker order id")
            if self.state is OrderState.FILLED:
                return self._updated(
                    event,
                    broker_order_id=confirmed_id,
                    anomalies=self._with_anomaly("CANCEL_AFTER_FILLED"),
                )
            if self.state is OrderState.CANCELLED:
                return self._updated(event, broker_order_id=confirmed_id)
            return self._updated(
                event,
                state=OrderState.CANCELLED,
                broker_order_id=confirmed_id,
            )
        if isinstance(event, BrokerRejected):
            if self.state in TERMINAL_ORDER_STATES:
                return self._updated(
                    event,
                    anomalies=self._with_anomaly(f"REJECT_AFTER_{self.state.value}"),
                )
            return self._updated(event, state=OrderState.REJECTED)
        if isinstance(event, OrderExpired):
            return self._updated(event, state=OrderState.EXPIRED)
        raise DomainTypeError(f"unsupported order event: {event.__class__.__name__}")


__all__ = (
    "ORDER_TRANSITIONS",
    "TERMINAL_ORDER_STATES",
    "AppliedEventIdentity",
    "AppliedFill",
    "ExecutionIntent",
    "OrderAggregate",
    "OrderState",
)
