"""Capability-bound, lease-guarded real execution with monotonic time fences."""

from __future__ import annotations

import hashlib
import json
import time as time_module
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from time import sleep as _sleep
from typing import Protocol

from firmquant.domain.broker_facts import Side
from firmquant.domain.orders import OrderAggregate, OrderState
from firmquant.persistence.production_repository import MonotonicExecutionLedgerRepository
from firmquant.persistence.writer_lease import WriterLeaseLost
from firmquant.risk.capability import BrokerWriteCapability

from .controller import ExecutionController, ExecutionSessionResult
from .planner import ExecutionPlan, PlannedOrder
from .policy import FeeSchedule
from .write_outcome import WriteFailureClass, classify_write_failure

_OPEN_STATES = frozenset({OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED})


class LeaseGuard(Protocol):
    """Minimal ownership port required by live execution."""

    def check(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ExecutionDeadlines:
    """Portfolio-level absolute deadlines expressed only in monotonic seconds."""

    latest_new_submit: float
    latest_cancel_initiation: float
    absolute_completion: float

    def __post_init__(self) -> None:
        values = (
            self.latest_new_submit,
            self.latest_cancel_initiation,
            self.absolute_completion,
        )
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
            raise TypeError("execution deadlines must be numeric monotonic values")
        if self.latest_new_submit < 0:
            raise ValueError("execution deadlines cannot be negative")
        if not (
            self.latest_new_submit <= self.latest_cancel_initiation <= self.absolute_completion
        ):
            raise ValueError("execution deadlines must be ordered")


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
    """Real execution that is bounded by capability, lease ownership and monotonic deadlines."""

    def __init__(
        self,
        *,
        capability: BrokerWriteCapability,
        ledger: MonotonicExecutionLedgerRepository,
        fee_schedule: FeeSchedule,
        clock: Callable[[], datetime],
        window_policy: ExecutionWindowPolicy,
        lease_guard: LeaseGuard | None = None,
        monotonic_clock: Callable[[], float] = time_module.monotonic,
        execution_deadlines: ExecutionDeadlines | None = None,
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
        if lease_guard is None or not callable(getattr(lease_guard, "check", None)):
            raise TypeError("live execution requires lease guard")
        if not callable(monotonic_clock):
            raise TypeError("live execution requires monotonic clock")
        if not isinstance(execution_deadlines, ExecutionDeadlines):
            raise TypeError("live execution requires ExecutionDeadlines")
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
        self._lease_guard = lease_guard
        self._monotonic_clock = monotonic_clock
        self._deadlines = execution_deadlines
        self._sleep = sleep
        self._last_monotonic: float | None = None

    def _monotonic(self) -> float:
        observed = self._monotonic_clock()
        if isinstance(observed, bool) or not isinstance(observed, (int, float)):
            raise RuntimeError("MONOTONIC_CLOCK_INVALID")
        value = float(observed)
        if value < 0 or (self._last_monotonic is not None and value < self._last_monotonic):
            raise RuntimeError("MONOTONIC_CLOCK_ROLLBACK")
        self._last_monotonic = value
        return value

    def _guard(self, aggregate: OrderAggregate | None = None) -> None:
        try:
            self._lease_guard.check()
        except WriterLeaseLost:
            self._cancel_only_after_lease_loss(aggregate)
            raise

    def _cancel_only_after_lease_loss(self, aggregate: OrderAggregate | None) -> None:
        """Best-effort broker-only cancel; durable state is recovered after restart."""

        if (
            aggregate is None
            or aggregate.state not in _OPEN_STATES
            or aggregate.broker_order_id is None
        ):
            return
        try:
            if self._monotonic() >= self._deadlines.latest_cancel_initiation:
                return
            self._capability.cancel_order(aggregate.broker_order_id)
        except Exception:
            # No database write is legal after lease loss. Recovery must reconcile the broker truth.
            return

    @staticmethod
    def _failure_evidence(error: BaseException) -> str:
        reason_codes = getattr(error, "reason_codes", ())
        safe_reasons = tuple(str(item) for item in reason_codes) if isinstance(reason_codes, tuple) else ()
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
        self._guard()
        started_at = self._clock()
        with self._production_ledger.database.transaction():
            submitting, attempt = self._production_ledger.begin_submit(
                aggregate,
                command,
                started_at=started_at,
            )
        self._guard()
        try:
            response = self._capability.submit_order(command)
        except Exception as error:
            evidence = self._failure_evidence(error)
            self._guard()
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
        self._guard()
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
        self._guard(aggregate)
        started_at = self._clock()
        with self._production_ledger.database.transaction():
            cancelling, attempt = self._production_ledger.begin_cancel(
                aggregate,
                started_at=started_at,
            )
        self._guard(aggregate)
        try:
            response = self._capability.cancel_order(aggregate.broker_order_id)
        except Exception as error:
            evidence = self._failure_evidence(error)
            self._guard(aggregate)
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
        self._guard(aggregate)
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
        self._guard(aggregate)
        orders = tuple(
            item for item in self._capability.query_orders() if item.broker_order_id == broker_order_id
        )
        if not orders:
            return aggregate
        if len(orders) != 1:
            raise RuntimeError("multiple broker orders match one execution")
        fills = tuple(
            item for item in self._capability.query_fills() if item.broker_order_id == broker_order_id
        )
        self._guard(aggregate)
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
        submitted_monotonic: float,
        side: Side,
    ) -> OrderAggregate:
        order_deadline = min(
            submitted_monotonic + self._window_policy.window_for(side).total_seconds(),
            self._deadlines.latest_cancel_initiation,
        )
        poll_seconds = self._window_policy.poll_interval.total_seconds()
        current = aggregate
        while current.state in _OPEN_STATES:
            self._guard(current)
            now = self._monotonic()
            if now >= order_deadline or now >= self._deadlines.absolute_completion:
                break
            remaining = min(
                poll_seconds,
                order_deadline - now,
                self._deadlines.absolute_completion - now,
            )
            if remaining <= 0:
                break
            self._sleep(remaining)
            current = self._refresh_open_order(current)
        return current

    def _finish_open_order(
        self,
        aggregate: OrderAggregate,
        *,
        submitted_monotonic: float,
        side: Side,
    ) -> OrderAggregate:
        current = self._wait_until_deadline(
            aggregate,
            submitted_monotonic=submitted_monotonic,
            side=side,
        )
        if current.state not in _OPEN_STATES:
            return current
        earliest_cancel = submitted_monotonic + self._window_policy.minimum_order_lifetime.total_seconds()
        now = self._monotonic()
        if now < earliest_cancel:
            remaining = min(
                earliest_cancel - now,
                self._deadlines.latest_cancel_initiation - now,
                self._deadlines.absolute_completion - now,
            )
            if remaining > 0:
                self._sleep(remaining)
                current = self._refresh_open_order(current)
        if current.state in _OPEN_STATES:
            now = self._monotonic()
            if (
                now < self._deadlines.latest_cancel_initiation
                and now < self._deadlines.absolute_completion
            ):
                current = self._cancel_live(current)
        return current

    def _safe_to_start(self, side: Side) -> bool:
        now = self._monotonic()
        if now >= self._deadlines.latest_new_submit:
            return False
        window_end = now + self._window_policy.window_for(side).total_seconds()
        return (
            window_end <= self._deadlines.latest_cancel_initiation
            and self._deadlines.latest_cancel_initiation <= self._deadlines.absolute_completion
        )

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
                    self._outcome(planned, None, reason_code="BLOCKED_BY_UNRESOLVED_UNKNOWN")
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
            if not self._safe_to_start(planned.side):
                outcomes.append(
                    self._outcome(
                        planned,
                        None,
                        reason_code="BLOCKED_BY_GLOBAL_EXECUTION_DEADLINE",
                    )
                )
                if planned.side is Side.SELL:
                    incomplete_sell = True
                continue

            shares, sizing_reason = self._shares_for_current_facts(planned)
            if shares <= 0:
                outcomes.append(self._outcome(planned, None, reason_code=sizing_reason))
                if planned.side is Side.SELL:
                    incomplete_sell = True
                    cash_after_sells = self._capability.query_account().available_cash
                continue

            submitted_monotonic = self._monotonic()
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
                    submitted_monotonic=submitted_monotonic,
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


__all__ = (
    "ExecutionDeadlines",
    "ExecutionWindowPolicy",
    "LeaseGuard",
    "LiveExecutionController",
)
