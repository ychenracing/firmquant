from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from firmquant.broker.fake import BrokerOperation, ScriptedOutcome
from firmquant.broker.gateway import BrokerHealth, BrokerOrderCommand
from firmquant.broker.normalization import normalize_order
from firmquant.domain.broker_facts import BrokerOrderStatus, MarketSessionStatus
from firmquant.domain.states import RuntimeState
from firmquant.execution.live_controller import (
    ExecutionDeadlines,
    ExecutionWindowPolicy,
    LiveExecutionController,
)
from firmquant.execution.planner import ExecutionPlanner
from firmquant.persistence.database import Database
from firmquant.persistence.production_repository import MonotonicExecutionLedgerRepository
from firmquant.persistence.repositories import DecisionSnapshotRepository
from firmquant.persistence.writer_lease import WriterLease, WriterLeaseGuard, WriterLeaseLost
from firmquant.risk.arm import ArmBinding, ArmService
from firmquant.risk.capability import (
    WriteAuthorizationContext,
    WriteCapabilityFactory,
    WriteOperation,
)
from firmquant.risk.gate import GateAction, GateDecision
from firmquant.scheduling.clock import ClockGuard, ClockObservation
from firmquant.security.secrets import SecretBytes
from tests.fixtures.session_cases import NOW, decision_snapshot, execution_snapshot
from tests.unit.execution.test_live_controller import MutableClock, fake_broker, fee_schedule, live_settings


class CountingGuard:
    def __init__(self) -> None:
        self.checks = 0

    def check(self) -> None:
        self.checks += 1


class FailingGuard:
    def check(self) -> None:
        raise WriterLeaseLost("test lease lost")


class MonotonicOnlyClock(MutableClock):
    def sleep(self, seconds: float) -> None:
        self.elapsed += seconds


def _capability(broker, clock: MutableClock):
    service = ArmService(
        mac_key=SecretBytes(b"test-only-arm-mac-key-material-32"),
        lease_id_factory=lambda: "arm_" + "e" * 32,
    )
    binding = ArmBinding.create(
        mode=live_settings().mode,
        hostname="host-a",
        account_id="account-a",
        firmquant_commit="f" * 40,
        uquant_commit="1" * 40,
        config_sha256="c" * 64,
    )
    lease = service.issue(
        binding,
        now=clock(),
        confirmation_reader=lambda: service.confirmation_phrase(live_settings().mode),
        interactive_terminal=True,
        environment={},
    )

    def source(operation: WriteOperation, subject: object | None) -> WriteAuthorizationContext:
        gate = None
        if isinstance(subject, BrokerOrderCommand):
            gate = GateDecision(
                action=GateAction.ALLOW,
                authorized_shares=subject.requested_shares,
                reason_codes=("ALL_CHECKS_PASSED",),
            )
        receipt = ClockGuard(max_drift=timedelta(seconds=2)).verify(
            ClockObservation(
                system_time=clock(),
                reference_time=clock(),
                local_timezone="Asia/Shanghai",
            )
        )
        return WriteAuthorizationContext(
            settings=live_settings(),
            lease=lease,
            binding=binding,
            now=clock(),
            runtime_state=RuntimeState.READY,
            broker_health=BrokerHealth(
                connected=True,
                read_healthy=True,
                write_healthy=True,
                observed_at=clock(),
                diagnostic_code="CONNECTED",
            ),
            startup_reconciliation_passed=True,
            broker_snapshot_received_at=clock(),
            max_broker_snapshot_age=timedelta(seconds=10),
            quote_received_at=clock(),
            max_quote_age=timedelta(seconds=5),
            session_valid=True,
            market_status=MarketSessionStatus.OPEN,
            fingerprints_match=True,
            kill_switch_tripped=False,
            unresolved_order_count=0,
            submitting_unresolved_count=0,
            reconciliation_mismatch=False,
            external_activity_detected=False,
            gate_decision=gate,
            cancel_risk_approved=True,
            symbol_in_canonical_universe=True,
            symbol_in_deployment_allowlist=True,
            command_within_uquant_intent=True,
            cash_and_positions_safe=True,
            frequency_within_limits=True,
            clock_receipt=receipt,
        )

    return WriteCapabilityFactory(arm_service=service).create(
        gateway=broker,
        context_provider=source,
    )


def _controller(
    database: Database,
    broker,
    clock: MutableClock,
    *,
    lease_guard,
    deadlines: ExecutionDeadlines,
) -> LiveExecutionController:
    return LiveExecutionController(
        capability=_capability(broker, clock),
        ledger=MonotonicExecutionLedgerRepository(database),
        fee_schedule=fee_schedule(),
        clock=clock,
        window_policy=ExecutionWindowPolicy(
            sell_window=timedelta(seconds=2),
            buy_window=timedelta(seconds=2),
            minimum_order_lifetime=timedelta(seconds=1),
            poll_interval=timedelta(seconds=1),
        ),
        lease_guard=lease_guard,
        monotonic_clock=clock.monotonic,
        execution_deadlines=deadlines,
        sleep=clock.sleep,
    )


def _script_open_then_cancelled(broker, plan) -> None:
    planned = plan.orders[0]
    accepted = normalize_order(
        {
            "broker_order_id": "runtime-time-order-1",
            "client_order_id": planned.uquant_order_id,
            "symbol": planned.symbol.canonical,
            "side": planned.side.value,
            "price_type": "LIMIT",
            "status": BrokerOrderStatus.ACKNOWLEDGED.value,
            "requested_shares": planned.uquant_authorized_shares.value,
            "filled_shares": 0,
            "limit_price": planned.limit_price.canonical,
            "session_date": plan.execution_session.isoformat(),
            "event_time": NOW.isoformat(),
            "event_sequence": 10,
        },
        received_at=NOW,
    )
    cancelled = replace(
        accepted,
        status=BrokerOrderStatus.CANCELLED,
        event_sequence=11,
    )
    broker.script(
        (
            ScriptedOutcome(BrokerOperation.SUBMIT, response=accepted),
            ScriptedOutcome(BrokerOperation.CANCEL, response=cancelled),
        )
    )


def test_long_order_wait_renews_writer_lease_on_poll_interval(tmp_path: Path) -> None:
    clock = MutableClock(NOW)
    writer = WriterLease.acquire(
        tmp_path / "firmquant.sqlite3",
        owner="long-order-keepalive",
        ttl=timedelta(seconds=5),
        clock=clock,
    )
    broker = fake_broker(clock)
    decision = decision_snapshot(include_sell=False, include_buy=True)
    plan = ExecutionPlanner().plan(decision, execution_snapshot())
    _script_open_then_cancelled(broker, plan)
    guard = WriterLeaseGuard(
        writer,
        monotonic_clock=clock.monotonic,
        renew_interval=timedelta(seconds=1),
    )
    initial_expiry = writer.expires_at
    try:
        with writer.database.transaction():
            DecisionSnapshotRepository(writer.database).append(decision)
        result = _controller(
            writer.database,
            broker,
            clock,
            lease_guard=guard,
            deadlines=ExecutionDeadlines(60.0, 90.0, 120.0),
        ).execute(plan)
        assert result.cancel_calls == 1
        assert writer.expires_at > initial_expiry
    finally:
        writer.release()


def test_keepalive_failure_prevents_any_new_submit(tmp_path: Path) -> None:
    clock = MutableClock(NOW)
    broker = fake_broker(clock)
    database = Database.open(tmp_path / "firmquant.sqlite3")
    decision = decision_snapshot(include_sell=False, include_buy=True)
    plan = ExecutionPlanner().plan(decision, execution_snapshot())
    try:
        with database.transaction():
            DecisionSnapshotRepository(database).append(decision)
        with pytest.raises(WriterLeaseLost, match="test lease lost"):
            _controller(
                database,
                broker,
                clock,
                lease_guard=FailingGuard(),
                deadlines=ExecutionDeadlines(60.0, 90.0, 120.0),
            ).execute(plan)
        assert broker.submitted_commands == ()
    finally:
        database.close()


def test_monotonic_order_window_expires_without_wall_clock_progress(tmp_path: Path) -> None:
    clock = MonotonicOnlyClock(NOW)
    broker = fake_broker(clock)
    database = Database.open(tmp_path / "firmquant.sqlite3")
    decision = decision_snapshot(include_sell=False, include_buy=True)
    plan = ExecutionPlanner().plan(decision, execution_snapshot())
    _script_open_then_cancelled(broker, plan)
    guard = CountingGuard()
    try:
        with database.transaction():
            DecisionSnapshotRepository(database).append(decision)
        result = _controller(
            database,
            broker,
            clock,
            lease_guard=guard,
            deadlines=ExecutionDeadlines(60.0, 90.0, 120.0),
        ).execute(plan)
        assert result.cancel_calls == 1
        assert clock.value == NOW
        assert clock.elapsed >= 2.0
        assert guard.checks >= 4
    finally:
        database.close()


def test_global_new_submit_deadline_blocks_before_broker_write(tmp_path: Path) -> None:
    clock = MutableClock(NOW)
    broker = fake_broker(clock)
    database = Database.open(tmp_path / "firmquant.sqlite3")
    decision = decision_snapshot(include_sell=False, include_buy=True)
    plan = ExecutionPlanner().plan(decision, execution_snapshot())
    try:
        with database.transaction():
            DecisionSnapshotRepository(database).append(decision)
        result = _controller(
            database,
            broker,
            clock,
            lease_guard=CountingGuard(),
            deadlines=ExecutionDeadlines(0.0, 10.0, 20.0),
        ).execute(plan)
        assert result.submit_calls == 0
        assert result.outcomes[0].reason_code == "BLOCKED_BY_GLOBAL_EXECUTION_DEADLINE"
        assert broker.submitted_commands == ()
    finally:
        database.close()
