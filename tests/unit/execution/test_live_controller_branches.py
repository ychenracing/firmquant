from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

import tests.unit.execution.test_live_controller as base
from firmquant.broker.fake import BrokerOperation, ScriptedOutcome
from firmquant.broker.normalization import normalize_order
from firmquant.domain.broker_facts import BrokerOrderStatus
from firmquant.domain.orders import OrderState
from firmquant.execution.live_controller import ExecutionWindowPolicy, LiveExecutionController
from firmquant.execution.planner import ExecutionPlanner
from firmquant.execution.write_outcome import BrokerWriteNotAccepted, BrokerWriteOutcomeUnknown
from firmquant.persistence.database import Database
from firmquant.persistence.production_repository import MonotonicExecutionLedgerRepository
from firmquant.persistence.repositories import DecisionSnapshotRepository
from tests.fixtures.session_cases import NOW, decision_snapshot, execution_snapshot


def _broker_fact(planned, *, broker_order_id: str, status: BrokerOrderStatus, sequence: int):
    return normalize_order(
        {
            "broker_order_id": broker_order_id,
            "client_order_id": planned.uquant_order_id,
            "symbol": planned.symbol.canonical,
            "side": planned.side.value,
            "price_type": "LIMIT",
            "status": status.value,
            "requested_shares": planned.uquant_authorized_shares.value,
            "filled_shares": 0,
            "limit_price": planned.limit_price.canonical,
            "session_date": planned.execution_session.isoformat(),
            "event_time": NOW.isoformat(),
            "event_sequence": sequence,
        },
        received_at=NOW,
    )


def _persist(database: Database, decision) -> None:
    with database.transaction():
        DecisionSnapshotRepository(database).append(decision)


def test_live_controller_constructor_and_window_policy_reject_invalid_dependencies(
    tmp_path: Path,
) -> None:
    clock = base.MutableClock(NOW)
    broker = base.fake_broker(clock)
    database = Database.open(tmp_path / "firmquant.sqlite3")
    ledger = MonotonicExecutionLedgerRepository(database)
    valid_policy = ExecutionWindowPolicy(
        sell_window=timedelta(seconds=2),
        buy_window=timedelta(seconds=2),
        minimum_order_lifetime=timedelta(seconds=1),
        poll_interval=timedelta(seconds=1),
    )
    try:
        with pytest.raises(TypeError, match="BrokerWriteCapability"):
            LiveExecutionController(
                capability=object(),  # type: ignore[arg-type]
                ledger=ledger,
                fee_schedule=base.fee_schedule(),
                clock=clock,
                window_policy=valid_policy,
            )
        with pytest.raises(TypeError, match="monotonic"):
            LiveExecutionController(
                capability=base.capability(broker, clock),
                ledger=object(),  # type: ignore[arg-type]
                fee_schedule=base.fee_schedule(),
                clock=clock,
                window_policy=valid_policy,
            )
        with pytest.raises(TypeError, match="ExecutionWindowPolicy"):
            LiveExecutionController(
                capability=base.capability(broker, clock),
                ledger=ledger,
                fee_schedule=base.fee_schedule(),
                clock=clock,
                window_policy=object(),  # type: ignore[arg-type]
            )
        with pytest.raises(TypeError, match="sleep"):
            LiveExecutionController(
                capability=base.capability(broker, clock),
                ledger=ledger,
                fee_schedule=base.fee_schedule(),
                clock=clock,
                window_policy=valid_policy,
                sleep=None,  # type: ignore[arg-type]
            )
        with pytest.raises(ValueError):
            ExecutionWindowPolicy(
                sell_window=timedelta(0),
                buy_window=timedelta(seconds=1),
                minimum_order_lifetime=timedelta(seconds=1),
                poll_interval=timedelta(seconds=1),
            )
        with pytest.raises(ValueError, match="poll interval"):
            ExecutionWindowPolicy(
                sell_window=timedelta(seconds=1),
                buy_window=timedelta(seconds=2),
                minimum_order_lifetime=timedelta(seconds=1),
                poll_interval=timedelta(seconds=3),
            )
    finally:
        database.close()


def test_submit_outcome_unknown_is_durable_and_blocks_later_orders(tmp_path: Path) -> None:
    clock = base.MutableClock(NOW)
    broker = base.fake_broker(clock)
    database = Database.open(tmp_path / "firmquant.sqlite3")
    decision = decision_snapshot(include_sell=True, include_buy=True)
    plan = ExecutionPlanner().plan(decision, execution_snapshot())
    broker.script(
        (
            ScriptedOutcome(
                BrokerOperation.SUBMIT,
                error=BrokerWriteOutcomeUnknown("response lost"),
            ),
        )
    )
    try:
        _persist(database, decision)
        result = base.controller(database, broker, clock).execute(plan)
        assert result.outcomes[0].final_state is OrderState.UNKNOWN
        assert result.outcomes[0].reason_code == "UNRESOLVED_UNKNOWN"
        assert result.outcomes[1].reason_code == "BLOCKED_BY_UNRESOLVED_UNKNOWN"
        assert result.unresolved_unknown is True
        assert result.submit_calls == 1
    finally:
        database.close()


def test_incomplete_sell_reduction_blocks_following_buy(tmp_path: Path) -> None:
    clock = base.MutableClock(NOW)
    broker = base.fake_broker(clock)
    database = Database.open(tmp_path / "firmquant.sqlite3")
    decision = decision_snapshot(include_sell=True, include_buy=True)
    plan = ExecutionPlanner().plan(decision, execution_snapshot())
    sell = plan.orders[0]
    accepted = _broker_fact(
        sell,
        broker_order_id="sell-1",
        status=BrokerOrderStatus.ACKNOWLEDGED,
        sequence=10,
    )
    cancelled = replace(accepted, status=BrokerOrderStatus.CANCELLED, event_sequence=11)
    broker.script(
        (
            ScriptedOutcome(BrokerOperation.SUBMIT, response=accepted),
            ScriptedOutcome(BrokerOperation.CANCEL, response=cancelled),
        )
    )
    try:
        _persist(database, decision)
        result = base.controller(database, broker, clock).execute(plan)
        assert result.outcomes[0].final_state is OrderState.CANCELLED
        assert result.outcomes[1].reason_code == "BLOCKED_BY_INCOMPLETE_SELL_REDUCTION"
        assert result.submit_calls == 1
        assert result.cancel_calls == 1
    finally:
        database.close()


def test_cancel_not_accepted_leaves_open_order_incomplete_without_unknown(tmp_path: Path) -> None:
    clock = base.MutableClock(NOW)
    broker = base.fake_broker(clock)
    database = Database.open(tmp_path / "firmquant.sqlite3")
    decision = decision_snapshot(include_sell=False, include_buy=True)
    plan = ExecutionPlanner().plan(decision, execution_snapshot())
    planned = plan.orders[0]
    accepted = _broker_fact(
        planned,
        broker_order_id="buy-1",
        status=BrokerOrderStatus.ACKNOWLEDGED,
        sequence=10,
    )
    broker.script(
        (
            ScriptedOutcome(BrokerOperation.SUBMIT, response=accepted),
            ScriptedOutcome(BrokerOperation.CANCEL, error=BrokerWriteNotAccepted("no")),
        )
    )
    try:
        _persist(database, decision)
        result = base.controller(database, broker, clock).execute(plan)
        assert result.outcomes[0].reason_code == "EXECUTION_WINDOW_INCOMPLETE"
        assert result.unresolved_unknown is False
        assert result.submit_calls == 1
    finally:
        database.close()


def test_cancel_outcome_unknown_halts_economic_progress(tmp_path: Path) -> None:
    clock = base.MutableClock(NOW)
    broker = base.fake_broker(clock)
    database = Database.open(tmp_path / "firmquant.sqlite3")
    decision = decision_snapshot(include_sell=False, include_buy=True)
    plan = ExecutionPlanner().plan(decision, execution_snapshot())
    planned = plan.orders[0]
    accepted = _broker_fact(
        planned,
        broker_order_id="buy-2",
        status=BrokerOrderStatus.ACKNOWLEDGED,
        sequence=10,
    )
    broker.script(
        (
            ScriptedOutcome(BrokerOperation.SUBMIT, response=accepted),
            ScriptedOutcome(BrokerOperation.CANCEL, error=BrokerWriteOutcomeUnknown("lost")),
        )
    )
    try:
        _persist(database, decision)
        result = base.controller(database, broker, clock).execute(plan)
        assert result.outcomes[0].final_state is OrderState.UNKNOWN
        assert result.unresolved_unknown is True
    finally:
        database.close()


def test_second_execution_reuses_existing_economic_order_without_resubmit(tmp_path: Path) -> None:
    clock = base.MutableClock(NOW)
    broker = base.fake_broker(clock)
    database = Database.open(tmp_path / "firmquant.sqlite3")
    decision = decision_snapshot(include_sell=False, include_buy=True)
    plan = ExecutionPlanner().plan(decision, execution_snapshot())
    planned = plan.orders[0]
    accepted = _broker_fact(
        planned,
        broker_order_id="buy-existing",
        status=BrokerOrderStatus.ACKNOWLEDGED,
        sequence=10,
    )
    cancelled = replace(accepted, status=BrokerOrderStatus.CANCELLED, event_sequence=11)
    broker.script(
        (
            ScriptedOutcome(BrokerOperation.SUBMIT, response=accepted),
            ScriptedOutcome(BrokerOperation.CANCEL, response=cancelled),
        )
    )
    try:
        _persist(database, decision)
        executor = base.controller(database, broker, clock)
        first = executor.execute(plan)
        submitted = len(broker.submitted_commands)
        second = executor.execute(plan)
        assert first.outcomes[0].final_state is OrderState.CANCELLED
        assert second.outcomes[0].final_state is OrderState.CANCELLED
        assert len(broker.submitted_commands) == submitted
    finally:
        database.close()


def test_finish_open_order_honors_minimum_lifetime_before_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = base.MutableClock(NOW)
    broker = base.fake_broker(clock)
    database = Database.open(tmp_path / "firmquant.sqlite3")
    decision = decision_snapshot(include_sell=False, include_buy=True)
    plan = ExecutionPlanner().plan(decision, execution_snapshot())
    planned = plan.orders[0]
    accepted = _broker_fact(
        planned,
        broker_order_id="buy-min-life",
        status=BrokerOrderStatus.ACKNOWLEDGED,
        sequence=10,
    )
    cancelled = replace(accepted, status=BrokerOrderStatus.CANCELLED, event_sequence=11)
    broker.script(
        (
            ScriptedOutcome(BrokerOperation.SUBMIT, response=accepted),
            ScriptedOutcome(BrokerOperation.CANCEL, response=cancelled),
        )
    )
    try:
        _persist(database, decision)
        executor = base.controller(database, broker, clock)
        aggregate = executor._new_aggregate(
            planned,
            shares=planned.uquant_authorized_shares.value,
            occurred_at=NOW,
        )
        aggregate = executor._submit_live(aggregate, planned)
        monkeypatch.setattr(executor, "_wait_until_deadline", lambda current, **_kwargs: current)
        finished = executor._finish_open_order(
            aggregate,
            submitted_monotonic=clock.monotonic(),
            side=planned.side,
        )
        assert finished.state is OrderState.CANCELLED
        assert clock.value >= NOW + timedelta(seconds=1)
    finally:
        database.close()


def test_failure_evidence_does_not_serialize_exception_message(tmp_path: Path) -> None:
    class WithReasons(RuntimeError):
        reason_codes = ("A", "B")

    class BadReasons(RuntimeError):
        def __init__(self, message: str) -> None:
            self.reason_codes = ["SECRET"]
            super().__init__(message)

    first = LiveExecutionController._failure_evidence(WithReasons("sensitive-a"))
    second = LiveExecutionController._failure_evidence(WithReasons("sensitive-b"))
    assert first == second
    assert LiveExecutionController._failure_evidence(BadReasons("sensitive")) != ""
