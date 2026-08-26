"""Durable SELL-first execution controller with unknown-outcome containment."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation

from firmquant.broker.gateway import BrokerGateway, BrokerOrderCommand
from firmquant.domain.broker_facts import BrokerFillFact, PriceType, Side
from firmquant.domain.errors import DomainTypeError, DomainValidationError
from firmquant.domain.orders import ExecutionIntent, OrderAggregate, OrderState
from firmquant.domain.values import Money, Shares
from firmquant.persistence.repositories import ExecutionLedgerRepository

from .planner import ExecutionPlan, PlannedOrder
from .policy import FeeSchedule

_MONEY_QUANTUM = Decimal("0.0001")


def _money(value: Decimal, *, label: str) -> Money:
    try:
        return Money(value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP))
    except InvalidOperation as error:
        raise DomainValidationError(f"{label} exceeds Decimal bounds") from error


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    uquant_order_id: str
    execution_id: str | None
    broker_order_id: str | None
    symbol: str
    side: Side
    uquant_authorized_shares: Shares
    submitted_shares: Shares
    submitted_value: Money
    filled_shares: Shares
    final_state: OrderState | None
    reason_code: str
    submit_attempts: int
    cancel_requests: int


@dataclass(frozen=True, slots=True)
class ExecutionSessionResult:
    plan_id: str
    decision_id: str
    outcomes: tuple[ExecutionOutcome, ...]
    opening_cash: Money
    cash_after_sells: Money
    ending_cash: Money
    negative_cash: bool
    unresolved_unknown: bool
    submit_calls: int
    cancel_calls: int
    reconciliation_required: bool = True
    ready_for_report: bool = False


class ExecutionController:
    """Persist commands before broker writes and never retry unresolved submission facts."""

    def __init__(
        self,
        *,
        gateway: BrokerGateway,
        ledger: ExecutionLedgerRepository,
        fee_schedule: FeeSchedule,
        clock: Callable[[], datetime],
        cancel_open_orders_at_end: bool,
    ) -> None:
        if not isinstance(gateway, BrokerGateway):
            raise DomainTypeError("execution gateway must satisfy BrokerGateway")
        if not isinstance(ledger, ExecutionLedgerRepository):
            raise DomainTypeError("execution ledger must be ExecutionLedgerRepository")
        if not isinstance(fee_schedule, FeeSchedule):
            raise DomainTypeError("execution fee schedule must be FeeSchedule")
        if not callable(clock):
            raise DomainTypeError("execution clock must be callable")
        if not isinstance(cancel_open_orders_at_end, bool):
            raise DomainTypeError("cancel_open_orders_at_end must be bool")
        self._gateway = gateway
        self._ledger = ledger
        self._fee_schedule = fee_schedule
        self._clock = clock
        self._cancel_open_orders_at_end = cancel_open_orders_at_end

    def _shares_for_current_facts(self, planned: PlannedOrder) -> tuple[int, str]:
        positions = {position.symbol: position for position in self._gateway.query_positions()}
        position = positions.get(planned.symbol)
        current = 0 if position is None else position.total_shares.value
        authorization = planned.uquant_authorized_shares.value
        unit = planned.trading_unit.value
        if planned.side is Side.SELL:
            if position is None:
                return 0, "POSITION_MISSING"
            candidate = min(
                authorization,
                position.total_shares.value,
                position.sellable_shares.value,
            )
            if candidate == position.total_shares.value and planned.target_shares.value == 0:
                return candidate, "AUTHORIZED"
            candidate -= candidate % unit
            return (candidate, "SELLABLE_SHRINK") if candidate < authorization else (candidate, "AUTHORIZED")
        target_gap = max(0, planned.target_shares.value - current)
        candidate = min(authorization, target_gap)
        candidate -= candidate % unit
        if candidate <= 0:
            return 0, "TARGET_ALREADY_SATISFIED"
        cash = self._gateway.query_account().available_cash.value
        maximum_by_gross = int((cash / planned.limit_price.value).to_integral_value(rounding=ROUND_FLOOR))
        candidate = min(candidate, maximum_by_gross)
        candidate -= candidate % unit
        authorized_candidate = candidate
        while candidate > 0:
            shares = Shares(candidate)
            fees = self._fee_schedule.calculate(
                side=Side.BUY,
                price=planned.limit_price,
                shares=shares,
            )
            if planned.limit_price.value * candidate + fees.total.value <= cash:
                break
            candidate -= unit
        if candidate <= 0:
            return 0, "CASH_INSUFFICIENT"
        if candidate < authorization or candidate < authorized_candidate:
            return candidate, "ACTUAL_CASH_SHRINK"
        return candidate, "AUTHORIZED"

    @staticmethod
    def _command(intent: ExecutionIntent, planned: PlannedOrder) -> BrokerOrderCommand:
        return BrokerOrderCommand(
            execution_id=intent.execution_id,
            idempotency_key=intent.idempotency_key,
            client_order_id=intent.uquant_order_id,
            symbol=intent.symbol,
            side=intent.side,
            price_type=PriceType.LIMIT,
            requested_shares=intent.requested_shares,
            limit_price=planned.limit_price,
            strategy_session=intent.strategy_session,
        )

    @staticmethod
    def _reason_for_state(aggregate: OrderAggregate) -> str:
        if aggregate.state is OrderState.UNKNOWN:
            return "UNRESOLVED_UNKNOWN"
        if aggregate.state is OrderState.CANCELLED:
            return "DEADLINE_CANCELLED"
        if aggregate.state is OrderState.FILLED:
            return "FILLED"
        if aggregate.state is OrderState.PARTIALLY_FILLED:
            return "PARTIAL_FILL"
        if aggregate.state is OrderState.REJECTED:
            return "BROKER_REJECTED"
        return aggregate.state.value

    @staticmethod
    def _outcome(
        planned: PlannedOrder,
        aggregate: OrderAggregate | None,
        *,
        reason_code: str,
    ) -> ExecutionOutcome:
        if aggregate is None:
            submitted = Shares(0)
            submitted_value = Money(Decimal(0))
            return ExecutionOutcome(
                uquant_order_id=planned.uquant_order_id,
                execution_id=None,
                broker_order_id=None,
                symbol=planned.symbol.canonical,
                side=planned.side,
                uquant_authorized_shares=planned.uquant_authorized_shares,
                submitted_shares=submitted,
                submitted_value=submitted_value,
                filled_shares=Shares(0),
                final_state=None,
                reason_code=reason_code,
                submit_attempts=0,
                cancel_requests=0,
            )
        submitted = aggregate.intent.requested_shares
        return ExecutionOutcome(
            uquant_order_id=planned.uquant_order_id,
            execution_id=aggregate.intent.execution_id,
            broker_order_id=aggregate.broker_order_id,
            symbol=planned.symbol.canonical,
            side=planned.side,
            uquant_authorized_shares=planned.uquant_authorized_shares,
            submitted_shares=submitted,
            submitted_value=_money(
                planned.limit_price.value * submitted.value,
                label="submitted order value",
            ),
            filled_shares=aggregate.filled_shares,
            final_state=aggregate.state,
            reason_code=reason_code,
            submit_attempts=aggregate.submit_attempts,
            cancel_requests=aggregate.cancel_requests,
        )

    def _new_aggregate(self, planned: PlannedOrder, *, shares: int, occurred_at: datetime) -> OrderAggregate:
        intent = ExecutionIntent.create(
            decision_id=planned.decision_id,
            uquant_order_id=planned.uquant_order_id,
            symbol=planned.symbol,
            side=planned.side,
            requested_shares=Shares(shares),
            strategy_session=planned.strategy_session,
            uquant_source_sha=planned.uquant_source_sha,
        )
        with self._ledger.database.transaction():
            aggregate = self._ledger.append_intent(intent, created_at=occurred_at)
            return self._ledger.validate_and_arm(aggregate, occurred_at=occurred_at)

    def _broker_fills(self, broker_order_id: str) -> tuple[BrokerFillFact, ...]:
        return tuple(fill for fill in self._gateway.query_fills() if fill.broker_order_id == broker_order_id)

    def _submit(self, aggregate: OrderAggregate, command: BrokerOrderCommand) -> tuple[OrderAggregate, int]:
        started_at = self._clock()
        with self._ledger.database.transaction():
            submitting, attempt = self._ledger.begin_submit(aggregate, command, started_at=started_at)
        try:
            response = self._gateway.submit_order(command)
        except Exception:
            with self._ledger.database.transaction():
                unknown = self._ledger.mark_attempt_unknown(
                    submitting,
                    attempt,
                    diagnostic_code="SUBMIT_CALL_OUTCOME_UNKNOWN",
                    occurred_at=self._clock(),
                )
            return unknown, 1
        try:
            fills = self._broker_fills(response.broker_order_id)
        except Exception:
            fills = ()
        with self._ledger.database.transaction():
            returned = self._ledger.record_submit_result(
                submitting,
                attempt,
                response,
                fills,
                received_at=self._clock(),
            )
        return returned, 1

    def _cancel(self, aggregate: OrderAggregate) -> tuple[OrderAggregate, int]:
        if aggregate.broker_order_id is None:
            raise DomainValidationError("cannot cancel without broker order id")
        started_at = self._clock()
        with self._ledger.database.transaction():
            cancelling, attempt = self._ledger.begin_cancel(aggregate, started_at=started_at)
        try:
            response = self._gateway.cancel_order(aggregate.broker_order_id)
        except Exception:
            with self._ledger.database.transaction():
                unknown = self._ledger.mark_attempt_unknown(
                    cancelling,
                    attempt,
                    diagnostic_code="CANCEL_CALL_OUTCOME_UNKNOWN",
                    occurred_at=self._clock(),
                )
            return unknown, 1
        try:
            fills = self._broker_fills(response.broker_order_id)
        except Exception:
            fills = ()
        with self._ledger.database.transaction():
            returned = self._ledger.record_cancel_result(
                cancelling,
                attempt,
                response,
                fills,
                received_at=self._clock(),
            )
        return returned, 1

    def execute(self, plan: ExecutionPlan) -> ExecutionSessionResult:
        if not isinstance(plan, ExecutionPlan):
            raise DomainTypeError("execution controller requires ExecutionPlan")
        opening_cash = self._gateway.query_account().available_cash
        cash_after_sells = opening_cash
        outcomes: list[ExecutionOutcome] = []
        unresolved_unknown = False
        submit_calls = 0
        cancel_calls = 0
        for planned in plan.orders:
            if unresolved_unknown:
                outcomes.append(self._outcome(planned, None, reason_code="BLOCKED_BY_UNRESOLVED_UNKNOWN"))
                continue
            existing = self._ledger.find_economic_order(
                decision_id=planned.decision_id,
                uquant_order_id=planned.uquant_order_id,
            )
            if existing is not None:
                reason = self._reason_for_state(existing)
                outcomes.append(self._outcome(planned, existing, reason_code=reason))
                if existing.state is OrderState.UNKNOWN:
                    unresolved_unknown = True
                if planned.side is Side.SELL:
                    cash_after_sells = self._gateway.query_account().available_cash
                continue
            shares, sizing_reason = self._shares_for_current_facts(planned)
            if shares <= 0:
                outcomes.append(self._outcome(planned, None, reason_code=sizing_reason))
                if planned.side is Side.SELL:
                    cash_after_sells = self._gateway.query_account().available_cash
                continue
            now = self._clock()
            aggregate = self._new_aggregate(planned, shares=shares, occurred_at=now)
            command = self._command(aggregate.intent, planned)
            aggregate, submit_increment = self._submit(aggregate, command)
            submit_calls += submit_increment
            if aggregate.state is OrderState.UNKNOWN:
                unresolved_unknown = True
            elif self._cancel_open_orders_at_end and aggregate.state in {
                OrderState.ACKNOWLEDGED,
                OrderState.PARTIALLY_FILLED,
            }:
                aggregate, cancel_increment = self._cancel(aggregate)
                cancel_calls += cancel_increment
                if aggregate.state is OrderState.UNKNOWN:
                    unresolved_unknown = True
            outcome_reason = (
                sizing_reason
                if aggregate.state is OrderState.FILLED and sizing_reason != "AUTHORIZED"
                else self._reason_for_state(aggregate)
            )
            outcomes.append(self._outcome(planned, aggregate, reason_code=outcome_reason))
            if planned.side is Side.SELL:
                cash_after_sells = self._gateway.query_account().available_cash
        ending_cash = self._gateway.query_account().available_cash
        return ExecutionSessionResult(
            plan_id=plan.plan_id,
            decision_id=plan.decision_id,
            outcomes=tuple(outcomes),
            opening_cash=opening_cash,
            cash_after_sells=cash_after_sells,
            ending_cash=ending_cash,
            negative_cash=ending_cash.value < 0,
            unresolved_unknown=unresolved_unknown,
            submit_calls=submit_calls,
            cancel_calls=cancel_calls,
        )


__all__ = ("ExecutionController", "ExecutionOutcome", "ExecutionSessionResult")
