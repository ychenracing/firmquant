from __future__ import annotations

import inspect
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import firmquant.scheduling.clock as scheduling_clock
from firmquant.application.operations import LocalOperatorService, OperatorCommand
from firmquant.application.production_daemon import ProductionHeartbeat
from firmquant.broker.production_factory import ReadOnlyXtQuantGateway
from firmquant.broker.production_smoke import ReadOnlyProductionSmokeBroker
from firmquant.config import ExecutionRuntimeSettings
from firmquant.execution.live_controller import ExecutionDeadlines, LiveExecutionController
from firmquant.persistence.writer_lease import (
    WriterLease,
    WriterLeaseGuard,
    WriterLeaseLost,
)
from firmquant.risk.gate import ExecutionRiskContext, RiskLimits
from firmquant.scheduling.clock import (
    RuntimeClockDiscontinuity,
    RuntimeClockMonitor,
    RuntimeClockObservation,
)


def test_expired_writer_lease_cannot_renew(tmp_path: Path) -> None:
    started = datetime(2026, 8, 27, 1, tzinfo=UTC)
    current = started
    lease = WriterLease.acquire(
        tmp_path / "firmquant.sqlite3",
        owner="expired-renew",
        ttl=timedelta(seconds=5),
        clock=lambda: current,
    )
    try:
        current = started + timedelta(seconds=5)
        with pytest.raises(WriterLeaseLost, match="expired"):
            lease.renew()
    finally:
        lease.release()


def test_writer_generation_takeover_cannot_renew(tmp_path: Path) -> None:
    now = datetime(2026, 8, 27, 1, tzinfo=UTC)
    lease = WriterLease.acquire(
        tmp_path / "firmquant.sqlite3",
        owner="generation-owner",
        ttl=timedelta(seconds=30),
        clock=lambda: now,
    )
    try:
        with lease.database.transaction("EXCLUSIVE"):
            lease.database.write(
                "UPDATE writer_leases SET generation = generation + 1 WHERE singleton_id = 1"
            )
        with pytest.raises(WriterLeaseLost):
            lease.renew()
    finally:
        lease.release()


def test_writer_lease_guard_renews_on_monotonic_interval_and_never_revives(tmp_path: Path) -> None:
    started = datetime(2026, 8, 27, 1, tzinfo=UTC)
    wall = [started]
    monotonic = [0.0]
    lease = WriterLease.acquire(
        tmp_path / "firmquant.sqlite3",
        owner="keepalive",
        ttl=timedelta(seconds=5),
        clock=lambda: wall[0],
    )
    guard = WriterLeaseGuard(
        lease,
        monotonic_clock=lambda: monotonic[0],
        renew_interval=timedelta(seconds=1),
    )
    try:
        wall[0] = started + timedelta(seconds=1)
        monotonic[0] = 1.0
        guard.check()
        assert lease.expires_at == started + timedelta(seconds=6)

        wall[0] = started + timedelta(seconds=6)
        monotonic[0] = 2.0
        with pytest.raises(WriterLeaseLost, match="expired"):
            guard.check()
        wall[0] = started + timedelta(seconds=2)
        monotonic[0] = 3.0
        with pytest.raises(WriterLeaseLost, match="previously lost"):
            guard.check()
    finally:
        lease.release()


def test_live_controller_requires_keepalive_monotonic_clock_and_global_deadlines() -> None:
    parameters = inspect.signature(LiveExecutionController).parameters
    assert {"lease_guard", "monotonic_clock", "execution_deadlines"} <= set(parameters)
    deadlines = ExecutionDeadlines(10.0, 20.0, 30.0)
    assert deadlines.latest_new_submit < deadlines.latest_cancel_initiation
    assert deadlines.latest_cancel_initiation < deadlines.absolute_completion
    with pytest.raises(ValueError):
        ExecutionDeadlines(20.0, 10.0, 30.0)


def test_clock_module_exposes_reference_provider_and_discontinuity_monitor() -> None:
    assert hasattr(scheduling_clock, "ClockReferenceProvider")
    assert hasattr(scheduling_clock, "RuntimeClockMonitor")
    assert hasattr(scheduling_clock, "MonotonicDeadline")


def test_runtime_clock_monitor_detects_wall_rollback_and_sleep_resume_gap() -> None:
    started = datetime(2026, 8, 27, 1, tzinfo=UTC)
    rollback = RuntimeClockMonitor(
        max_observation_gap=timedelta(seconds=5),
        max_wall_monotonic_divergence=timedelta(seconds=1),
    )
    rollback.observe(RuntimeClockObservation(started, 0.0))
    with pytest.raises(RuntimeClockDiscontinuity, match="WALL_CLOCK_ROLLBACK"):
        rollback.observe(RuntimeClockObservation(started - timedelta(seconds=2), 1.0))

    resume = RuntimeClockMonitor(
        max_observation_gap=timedelta(seconds=5),
        max_wall_monotonic_divergence=timedelta(seconds=1),
    )
    resume.observe(RuntimeClockObservation(started, 0.0))
    with pytest.raises(RuntimeClockDiscontinuity, match="RUNTIME_OBSERVATION_GAP"):
        resume.observe(RuntimeClockObservation(started + timedelta(seconds=10), 10.0))


def test_heartbeat_contract_contains_verifiable_process_and_authority_facts() -> None:
    names = {field.name for field in fields(ProductionHeartbeat)}
    assert {
        "runtime_state",
        "host_hash",
        "process_id",
        "writer_generation",
        "broker_connected",
        "broker_read_healthy",
        "broker_write_healthy",
        "pending_events",
        "last_broker_event",
        "last_quote",
        "last_reconciliation",
        "last_decision",
        "last_execution",
        "control_request_state",
    } <= names


def test_persistent_heartbeat_schema_is_installed(tmp_path: Path) -> None:
    lease = WriterLease.acquire(tmp_path / "firmquant.sqlite3", owner="heartbeat-schema")
    try:
        row = lease.database.query_one(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'production_heartbeat'"
        )
        assert row is not None
    finally:
        lease.release()


def test_status_contract_does_not_report_unknown_broker_placeholder() -> None:
    source = inspect.getsource(LocalOperatorService._status_snapshot)
    assert '"broker_connection": "UNKNOWN"' not in source
    assert "heartbeat_age" in source
    assert "STALE" in source
    assert "NOT_RUNNING" in source


def test_production_diagnostic_types_have_no_broker_write_surface() -> None:
    assert not hasattr(ReadOnlyXtQuantGateway, "submit_order")
    assert not hasattr(ReadOnlyXtQuantGateway, "cancel_order")
    assert not hasattr(ReadOnlyProductionSmokeBroker, "submit_order")
    assert not hasattr(ReadOnlyProductionSmokeBroker, "cancel_order")


def test_readonly_smoke_is_explicit_operator_command() -> None:
    assert "smoke-readonly" in {command.value for command in OperatorCommand}


def test_replacement_configuration_and_risk_fields_are_removed() -> None:
    assert "max_replacements" not in ExecutionRuntimeSettings.model_fields
    assert "max_replacements" not in {field.name for field in fields(RiskLimits)}
    assert "replacement_count" not in {field.name for field in fields(ExecutionRiskContext)}


def test_production_risk_wiring_has_no_favourable_placeholder_literals() -> None:
    from firmquant.application.production_services import ProductionServiceHooks

    source = inspect.getsource(ProductionServiceHooks)
    forbidden = (
        "existing_order_age=None",
        "replacement_count=0",
        "unexplained_position_change=False",
        "corporate_action_suspected=False",
        "clock_drift=timedelta(0)",
        "reconciliation_mismatch=False",
        "cash_and_positions_safe=True",
    )
    assert not [literal for literal in forbidden if literal in source]
