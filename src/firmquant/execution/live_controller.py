"""Capability-bound, time-bounded real execution with explicit write outcomes."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from time import sleep as _sleep

from firmquant.domain.broker_facts import Side
from firmquant.domain.orders import OrderAggregate, OrderState
from firmquant.persistence.production_repository import MonotonicExecutionLedgerRepository
from firmquant.risk.capability import BrokerWriteCapability

from .controller import ExecutionController, ExecutionSessionResult
from .planner import ExecutionPlan, PlannedOrder
from .policy import FeeSchedule
from .write_outcome import WriteFailureClass, classify_write_failure

_OPEN_STATES = frozenset({OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED})


@dataclass(frozen=True, slots=True)
class ExecutionWindowPolicy:
    """Finite broker exposure window; never implies a retry or price-expansion policy."""

    sell_window: timedelta
    buy_window: timedelta
    minimum_order_lifetime: timedelta
    poll_interval: timedelta

    def __post_init__(self) -> None:
        for label, value in (
            ("sell window", self.sell_window),
            ("buy window", self.buy_window),
            ("minimum order lifetime", self.minimum_order_lifetime),
            ("poll interval", self.poll_interval),
        ):
            if not isinstance(value, timedelta) or value <= timedelta(0):
                raise ValueError(f"{label} must be a positive timedelta")
        if self.minimum_order_lifetime > min(self.sell_window, self.buy_window):
            raise ValueError("minimum order lifetime must fit every execution window")
        if self.poll_interval > max(self.sell_window, self.buy_window):
            raise ValueError("poll interval cannot exceed the longest execution window")

    def window_for(self, side: Side) -> timedelta:
        if not isinstance(side, Side):
            raise TypeError("execution window side must be Side")
        return self.sell_window if side is Side.SELL else self.buy_window


class LiveExecutionController(ExecutionController):
    """Real execution that cannot be constructed without dynamic broker-write capability."""

    def __init__(
        self,
        *,
        capability: BrokerWriteCapability,
        ledger: MonotonicExecutionLedgerRepository,
        fee_schedule: FeeSchedule,
        clock: Callable[[], datetime],
        window_policy: ExecutionWindowPolicy,
        sleep: Callable[[float], None] = _sleep,
    ) -> None:
        if not isinstance(capability, BrokerWriteCapability):
            raise TypeError("live execution requires BrokerWriteCapability")
        if not isinstance(ledger, MonotonicExecutionLedgerRepository):
            raise TypeError("live execution requires monotonic execution ledger")
        if not isinstance(window_policy, ExecutionWindowPolicy):
            raise TypeError("live execution requires ExecutionWindowPolicy")
        if not callable(sleep):
            raise TypeError("live execution sleep must be callable")
        super().__init__(
            gateway=capability,
            ledger=ledger,
            fee_schedule=fee_schedule,
            clock=clock,
            cancel_open_orders_at_end=False,
        )
        self._capability = capability
        self._production_ledger = ledger
        self._window_policy = window_policy
        self._sleep = sleep

    @staticmethod
    def _failure_evidence(error: BaseException) -> str:
        reason_codes = getattr(error, "reason_codes", ())
        safe_reasons = (
            tuple(str(item) for item in reason_codes)
            if isinstance(reason_codes, tuple)
            else ()
        )
        payload = json.dumps(
            {
                "schema": "firmquant.write-failure-evidence.v1",
                "error_type": type(error).__name__,
                "reason_codes": safe_reasons,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _submit_live(self, aggregate: OrderAggregate, planned: PlannedOrder) -> OrderAggregate:
        command = self._command(aggregate.intent, planned)
        started_at = self._clock()
        with self._production_ledger.database.transaction():
            submitting, attempt = self._production_ledger.begin_submit(
                aggregate,
                command,
                started_at=started_at,
            )
        try:
            response = self._capability.submit_order(command)
        except Exception as error:
            evidence = self._failure_evidence(error)
            with self._production_ledger.database.transaction():
                if classify_write_failure(error) is WriteFailureClass.NOT_ACCEPTED:
                    return self._production_ledger.resolve_submit_not_accepted(
                        submitting,
                        attempt,
                        evidence_sha256=evidence,
                        occurred_at=self._clock(),
                    )
                return self._production_ledger.mark_attempt_unknown(
                    submitting,
                    attempt,
                    diagnostic_code="SUBMIT_CALL_OUTCOME_UNKNOWN",
                    occurred_at=self._clock(),
                )
        try:
            fills = self._broker_fills(response.broker_order_id)
        except Exception:
            fills = ()
        with self._production_ledger.database.transaction():
            return self._production_ledger.record_submit_result(
                submitting,
                attempt,
                response,
                fills,
                received_at=self._clock(),
            )

    def _cancel_live(self, aggregate: OrderAggregate) -> OrderAggregate:
        if aggregate.broker_order_id is None:
            raise ValueError("cannot cancel live order without broker order id")
        started_at = self._clock()
        with self._production_ledger.database.transaction():
            cancelling, attempt = self._production_ledger.begin_cancel(
                aggregate,
                started_at=started_at,
            )
        try:
            response = self._capability.cancel_order(aggregate.broker_order_id)
        except Exception as error:
            evidence = self._failure_evidence(error)
            with self._production_ledger.database.transaction():
                if classify_write_failure(error) is WriteFailureClass.NOT_ACCEPTED:
                    return self._production_ledger.resolve_cancel_not_accepted(
                        cancelling,
                        attempt,
                        evidence_sha256=evidence,
                        occurred_at=self._clock(),
                    )
                return self._production_ledger.mark_attempt_unknown(
                    cancelling,
                    attempt,
                    diagnostic_code="CANCEL_CALL_OUTCOME_UNKNOWN",
                    occurred_at=self._clock(),
                )
        try:
            fills = self._broker_fills(response.broker_order_id)
        except Exception:
            fills = ()
        with self._production_ledger.database.transaction():
            return self._production_ledger.record_cancel_result(
                cancelling,
                attempt,
                response,
                fills,
                received_at=self._clock(),
            )

    def _refresh_open_order(self, aggregate: OrderAggregate) -> OrderAggregate:
        broker_order_id = aggregate.broker_order_id
        if broker_order_id is None:
            return aggregate
        orders = tuple(
            item
            for item in self._capability.query_orders()
            if item.broker_order_id == broker_order_id
        )
        if not orders:
            return aggregate
        if len(orders) != 1:
            raise RuntimeError("multiple broker orders match one execution")
        fills = tuple(
            item
            for item in self._capability.query_fills()
            if item.broker_order_id == broker_order_id
        )
        with self._production_ledger.database.transaction():
            return self._production_ledger.reconcile_broker_fact(
                aggregate,
                orders[0],
                fills,
                received_at=self._clock(),
            )

    def _wait_until_deadline(
        self,
        aggregate: OrderAggregate,
        *,
        submitted_at: datetime,
        side: Side,
    ) -> OrderAggregate:
        deadline = submitted_at + self._window_policy.window_for(side)
        poll_seconds = self._window_policy.poll_interval.total_seconds()
        window_seconds = self._window_policy.window_for(side).total_seconds()
        max_polls = max(1, math.ceil(window_seconds / poll_seconds) + 2)
        current = aggregate
        for _ in range(max_polls):
            if current.state not in _OPEN_STATES:
                return current
            now = self._clock()
            if now >= deadline:
                break
            remaining = max(0.0, (deadline - now).total_seconds())
            self._sleep(min(poll_seconds, remaining))
            current = self._refresh_open_order(current)
        return current

    def _finish_open_order(
        self,
        aggregate: OrderAggregate,
        *,
        submitted_at: datetime,
        side: Side,
    ) -> OrderAggregate:
        current = self._wait_until_deadline(
            aggregate,
            submitted_at=submitted_at,
            side=side,
        )
        if current.state not in _OPEN_STATES:
            return current
        earliest_cancel = submitted_at + self._window_policy.minimum_order_lifetime
        now = self._clock()
        if now < earliest_cancel:
            self._sleep((earliest_cancel - now).total_seconds())
            current = self._refresh_open_order(current)
        if current.state in _OPEN_STATES:
            current = self._cancel_live(current)
        return current

    def execute(self, plan: ExecutionPlan) -> ExecutionSessionResult:
        if not isinstance(plan, ExecutionPlan):
            raise TypeError("live execution requires ExecutionPlan")
        opening_cash = self._capability.query_account().available_cash
        cash_after_sells = opening_cash
        outcomes = []
        unresolved_unknown = False
        incomplete_sell = False
        submit_calls = 0
        cancel_calls = 0
        for planned in plan.orders:
            if unresolved_unknown:
                outcomes.append(
                    self._outcome(
                        planned,
                        None,
                        reason_code="BLOCKED_BY_UNRESOLVED_UNKNOWN",
                    )
                )
                continue
            if planned.side is Side.BUY and incomplete_sell:
                outcomes.append(
                    self._outcome(
                        planned,
                        None,
                        reason_code="BLOCKED_BY_INCOMPLETE_SELL_REDUCTION",
                    )
                )
                continue
            existing = self._production_ledger.find_economic_order(
                decision_id=planned.decision_id,
                uquant_order_id=planned.uquant_order_id,
            )
            if existing is not None:
                outcomes.append(
                    self._outcome(
                        planned,
                        existing,
                        reason_code=self._reason_for_state(existing),
                    )
                )
                unresolved_unknown = existing.state is OrderState.UNKNOWN
                if planned.side is Side.SELL:
                    incomplete_sell = existing.state is not OrderState.FILLED
                    cash_after_sells = self._capability.query_account().available_cash
                continue

            shares, sizing_reason = self._shares_for_current_facts(planned)
            if shares <= 0:
                outcomes.append(self._outcome(planned, None, reason_code=sizing_reason))
                if planned.side is Side.SELL:
                    incomplete_sell = True
                    cash_after_sells = self._capability.query_account().available_cash
                continue

            submitted_at = self._clock()
            aggregate = self._new_aggregate(planned, shares=shares, occurred_at=submitted_at)
            aggregate = self._submit_live(aggregate, planned)
            submit_calls += 1
            if aggregate.state is OrderState.ARMED:
                reason = "SUBMIT_NOT_ACCEPTED"
            elif aggregate.state is OrderState.UNKNOWN:
                unresolved_unknown = True
                reason = "UNRESOLVED_UNKNOWN"
            else:
                before_cancel_requests = aggregate.cancel_requests
                aggregate = self._finish_open_order(
                    aggregate,
                    submitted_at=submitted_at,
                    side=planned.side,
                )
                cancel_calls += aggregate.cancel_requests - before_cancel_requests
                unresolved_unknown = aggregate.state is OrderState.UNKNOWN
                reason = self._reason_for_state(aggregate)
                if aggregate.state in _OPEN_STATES:
                    reason = "EXECUTION_WINDOW_INCOMPLETE"
            outcomes.append(self._outcome(planned, aggregate, reason_code=reason))
            if planned.side is Side.SELL:
                incomplete_sell = aggregate.state is not OrderState.FILLED
                cash_after_sells = self._capability.query_account().available_cash

        ending_cash = self._capability.query_account().available_cash
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


__all__ = ("ExecutionWindowPolicy", "LiveExecutionController")
