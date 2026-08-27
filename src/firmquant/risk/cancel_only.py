"""Emergency broker cancellation capability that can only reduce open system-order risk."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Never, SupportsIndex

from firmquant.broker.gateway import BrokerGateway, _broker_write_authorization_scope
from firmquant.config import Mode
from firmquant.domain.broker_facts import BrokerOrderFact, BrokerOrderStatus
from firmquant.domain.errors import DomainTypeError
from firmquant.domain.orders import OrderState
from firmquant.persistence.account_authority import AccountBindingRepository
from firmquant.persistence.production_repository import MonotonicExecutionLedgerRepository

_CANCEL_ONLY_TOKEN: Final = object()
_ACTIVE_LOCAL_STATES: Final = frozenset({OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED})
_ACTIVE_BROKER_STATES: Final = frozenset(
    {
        BrokerOrderStatus.PENDING_NEW,
        BrokerOrderStatus.ACKNOWLEDGED,
        BrokerOrderStatus.PARTIALLY_FILLED,
    }
)


def _aware(value: datetime, *, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise DomainTypeError(f"{label} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


@dataclass(frozen=True, slots=True)
class CancelOnlyResult:
    """Exact outcome of one ledger-derived cancellation sweep."""

    cancelled_order_ids: tuple[str, ...] = ()
    terminal_order_ids: tuple[str, ...] = ()
    unknown_order_ids: tuple[str, ...] = ()
    denied_order_ids: tuple[str, ...] = ()
    cancel_calls: int = 0
    mode_write_forbidden: bool = False


@dataclass(frozen=True, slots=True)
class _Candidate:
    execution_id: str
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: str
    requested_shares: int
    filled_shares: int
    limit_price: str
    session_date: str
    event_sequence: int | None


class BrokerCancelOnlyCapability:
    """Opaque cancellation-only view. There is deliberately no submit_order method."""

    __slots__ = ("_clock", "_gateway", "_ledger", "_mode")

    def __init__(
        self,
        *,
        token: object,
        mode: Mode,
        gateway: BrokerGateway,
        ledger: MonotonicExecutionLedgerRepository,
        clock: Callable[[], datetime],
    ) -> None:
        if token is not _CANCEL_ONLY_TOKEN:
            raise RuntimeError("cancel-only capability factory is required")
        self._mode = mode
        self._gateway = gateway
        self._ledger = ledger
        self._clock = clock

    def cancel_system_orders(self) -> CancelOnlyResult:
        """Cancel only currently active SYSTEM orders proven by durable ledger identity."""

        if self._mode not in {Mode.CANARY, Mode.LIVE}:
            return CancelOnlyResult(mode_write_forbidden=True)

        cancelled: list[str] = []
        terminal: list[str] = []
        unknown: list[str] = []
        denied: list[str] = []
        cancel_calls = 0

        for candidate in self._candidates():
            aggregate = self._ledger.load(candidate.execution_id)
            if (
                aggregate is None
                or aggregate.state not in _ACTIVE_LOCAL_STATES
                or aggregate.broker_order_id != candidate.broker_order_id
            ):
                continue
            current = self._current_broker_order(candidate)
            if current is None:
                denied.append(candidate.broker_order_id)
                continue

            started_at = _aware(self._clock(), label="cancel-only clock")
            with self._ledger.database.transaction():
                cancelling, attempt = self._ledger.begin_cancel(aggregate, started_at=started_at)
            cancel_calls += 1
            try:
                with _broker_write_authorization_scope():
                    response = self._gateway.cancel_order(candidate.broker_order_id)
            except Exception:
                with self._ledger.database.transaction():
                    self._ledger.mark_attempt_unknown(
                        cancelling,
                        attempt,
                        diagnostic_code="CANCEL_CALL_OUTCOME_UNKNOWN",
                        occurred_at=_aware(self._clock(), label="cancel-only clock"),
                    )
                unknown.append(candidate.broker_order_id)
                continue

            try:
                fills = tuple(
                    fill
                    for fill in self._gateway.query_fills()
                    if fill.broker_order_id == candidate.broker_order_id
                )
            except Exception:
                fills = ()
            with self._ledger.database.transaction():
                result = self._ledger.record_cancel_result(
                    cancelling,
                    attempt,
                    response,
                    fills,
                    received_at=_aware(self._clock(), label="cancel-only clock"),
                )
            if result.state is OrderState.CANCELLED:
                cancelled.append(candidate.broker_order_id)
            elif result.state is OrderState.UNKNOWN:
                unknown.append(candidate.broker_order_id)
            elif result.state in {
                OrderState.FILLED,
                OrderState.REJECTED,
                OrderState.EXPIRED,
            }:
                terminal.append(candidate.broker_order_id)
            else:
                unknown.append(candidate.broker_order_id)

        return CancelOnlyResult(
            cancelled_order_ids=tuple(cancelled),
            terminal_order_ids=tuple(terminal),
            unknown_order_ids=tuple(unknown),
            denied_order_ids=tuple(denied),
            cancel_calls=cancel_calls,
        )

    def _candidates(self) -> tuple[_Candidate, ...]:
        rows = self._ledger.database.query_all(
            """
            SELECT
                b.execution_id,
                b.broker_order_id,
                b.client_order_id,
                b.symbol,
                b.side,
                b.requested_shares,
                b.filled_shares,
                b.limit_price,
                b.session_date,
                b.last_event_sequence
            FROM broker_orders AS b
            JOIN execution_intents AS e ON e.execution_id = b.execution_id
            WHERE b.ownership = 'SYSTEM'
              AND b.execution_id IS NOT NULL
              AND b.broker_order_id IS NOT NULL
              AND b.client_order_id IS NOT NULL
              AND b.limit_price IS NOT NULL
              AND e.state IN ('ACKNOWLEDGED', 'PARTIALLY_FILLED')
              AND b.status IN ('PENDING_NEW', 'ACKNOWLEDGED', 'PARTIALLY_FILLED')
            ORDER BY b.broker_order_id
            """
        )
        candidates: list[_Candidate] = []
        for row in rows:
            try:
                candidates.append(
                    _Candidate(
                        execution_id=str(row["execution_id"]),
                        broker_order_id=str(row["broker_order_id"]),
                        client_order_id=str(row["client_order_id"]),
                        symbol=str(row["symbol"]),
                        side=str(row["side"]),
                        requested_shares=int(row["requested_shares"]),
                        filled_shares=int(row["filled_shares"]),
                        limit_price=str(row["limit_price"]),
                        session_date=str(row["session_date"]),
                        event_sequence=(None if row["last_event_sequence"] is None else int(row["last_event_sequence"])),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return tuple(candidates)

    def _current_broker_order(self, candidate: _Candidate) -> BrokerOrderFact | None:
        try:
            health = self._gateway.health()
            if not health.connected or not health.read_healthy or not health.write_healthy:
                return None
            binding = AccountBindingRepository(self._ledger.database).load()
            if binding is None:
                return None
            account = self._gateway.query_account()
            if (
                account.account_id_hash != binding.account_id_hash
                or account.account_type is not binding.account_type
            ):
                return None
            matches = tuple(
                order
                for order in self._gateway.query_orders()
                if order.broker_order_id == candidate.broker_order_id
            )
        except Exception:
            return None
        if len(matches) != 1:
            return None
        order = matches[0]
        if order.status not in _ACTIVE_BROKER_STATES:
            return None
        if (
            order.client_order_id != candidate.client_order_id
            or order.symbol.canonical != candidate.symbol
            or order.side.value != candidate.side
            or order.requested_shares.value != candidate.requested_shares
            or order.limit_price.canonical != candidate.limit_price
            or order.session_date.isoformat() != candidate.session_date
            or order.filled_shares.value < candidate.filled_shares
        ):
            return None
        if candidate.event_sequence is not None and order.event_sequence < candidate.event_sequence:
            return None
        return order

    def __repr__(self) -> str:
        return "<BrokerCancelOnlyCapability opaque>"

    def __reduce_ex__(self, protocol: SupportsIndex, /) -> Never:
        del protocol
        raise TypeError("BrokerCancelOnlyCapability is not serializable")

    def __getstate__(self) -> Never:
        raise TypeError("BrokerCancelOnlyCapability is not serializable")


class CancelOnlyCapabilityFactory:
    """Factory for the deliberately submit-free emergency cancellation capability."""

    __slots__ = ("_mode",)

    def __init__(self, *, mode: Mode) -> None:
        if not isinstance(mode, Mode):
            raise DomainTypeError("cancel-only capability mode must be Mode")
        self._mode = mode

    def create(
        self,
        *,
        gateway: BrokerGateway,
        ledger: MonotonicExecutionLedgerRepository,
        clock: Callable[[], datetime],
    ) -> BrokerCancelOnlyCapability:
        if not isinstance(gateway, BrokerGateway):
            raise DomainTypeError("cancel-only gateway must satisfy BrokerGateway")
        if not isinstance(ledger, MonotonicExecutionLedgerRepository):
            raise DomainTypeError("cancel-only ledger must be monotonic production ledger")
        if not callable(clock):
            raise DomainTypeError("cancel-only clock must be callable")
        return BrokerCancelOnlyCapability(
            token=_CANCEL_ONLY_TOKEN,
            mode=self._mode,
            gateway=gateway,
            ledger=ledger,
            clock=clock,
        )


__all__ = (
    "BrokerCancelOnlyCapability",
    "CancelOnlyCapabilityFactory",
    "CancelOnlyResult",
)
