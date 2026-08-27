from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from firmquant.application.production_daemon import ProductionHeartbeat
from firmquant.broker.fake import FakeBroker
from firmquant.broker.production_smoke import (
    ProductionSmokeReceipt,
    ProductionSmokeStore,
    run_readonly_production_smoke,
)
from firmquant.config import Mode
from firmquant.domain.broker_facts import MarketSessionStatus
from firmquant.domain.states import RuntimeState
from firmquant.persistence.database import Database
from firmquant.persistence.writer_lease import (
    WriterLease,
    WriterLeaseBusy,
    WriterLeaseGuard,
    WriterLeaseLost,
    writer_lock_available,
)
from firmquant.scheduling.clock import (
    ClockGuard,
    ClockObservation,
    ClockValidationError,
    MonotonicDeadline,
    RuntimeClockDiscontinuity,
    RuntimeClockMonitor,
    RuntimeClockObservation,
)
from tests.fixtures.session_cases import execution_snapshot

NOW = datetime(2026, 8, 27, 1, tzinfo=UTC)


def _heartbeat() -> ProductionHeartbeat:
    return ProductionHeartbeat(
        mode=Mode.CANARY,
        runtime_state=RuntimeState.READY,
        observed_at=NOW,
        host_hash="a" * 64,
        process_id=1,
        writer_generation=1,
        broker_connected=True,
        broker_read_healthy=True,
        broker_write_healthy=True,
        pending_events=0,
        last_broker_event=NOW,
        last_quote=NOW,
        last_reconciliation=NOW,
        last_decision=NOW,
        last_execution=NOW,
        control_request_state="IDLE",
        processed_events=0,
        decisions=0,
        executions=0,
        eod=0,
    )


def test_heartbeat_validation_fails_closed_on_invalid_authority_facts() -> None:
    heartbeat = _heartbeat()
    bad_values = (
        {"mode": Mode.PAPER},
        {"runtime_state": "READY"},
        {"observed_at": NOW.replace(tzinfo=None)},
        {"host_hash": "bad"},
        {"process_id": True},
        {"pending_events": -1},
        {"broker_connected": 1},
        {"broker_read_healthy": False, "broker_write_healthy": True},
        {"last_quote": NOW.replace(tzinfo=None)},
        {"control_request_state": ""},
        {"control_request_state": " x "},
        {"control_request_state": "x" * 65},
    )
    for changes in bad_values:
        with pytest.raises((TypeError, ValueError)):
            replace(heartbeat, **changes)


def test_clock_value_objects_validate_all_fail_closed_edges() -> None:
    naive = NOW.replace(tzinfo=None)
    with pytest.raises(ClockValidationError):
        ClockObservation(naive, NOW, "Asia/Shanghai")
    with pytest.raises(ClockValidationError):
        ClockObservation(NOW, naive, "Asia/Shanghai")
    with pytest.raises(ClockValidationError):
        ClockObservation(NOW, NOW, " ")
    with pytest.raises(ClockValidationError):
        MonotonicDeadline(True)
    with pytest.raises(ClockValidationError):
        MonotonicDeadline(-1.0)
    deadline = MonotonicDeadline(10.0)
    assert deadline.remaining(lambda: 4.0) == 6.0
    assert deadline.remaining(lambda: 11.0) == 0.0
    assert deadline.expired(lambda: 10.0) is True
    with pytest.raises(ClockValidationError):
        deadline.remaining(lambda: True)
    with pytest.raises(ClockValidationError):
        deadline.expired(lambda: "bad")  # type: ignore[return-value]
    with pytest.raises(ClockValidationError):
        RuntimeClockObservation(naive, 0.0)
    with pytest.raises(ClockValidationError):
        RuntimeClockObservation(NOW, True)
    with pytest.raises(ClockValidationError):
        RuntimeClockObservation(NOW, -1.0)


def test_runtime_clock_monitor_rejects_invalid_and_divergent_observations() -> None:
    with pytest.raises(ValueError):
        RuntimeClockMonitor(max_observation_gap=timedelta(0))
    monitor = RuntimeClockMonitor(
        max_observation_gap=timedelta(seconds=30),
        max_wall_monotonic_divergence=timedelta(seconds=1),
    )
    with pytest.raises(ClockValidationError):
        monitor.observe(object())  # type: ignore[arg-type]
    monitor.observe(RuntimeClockObservation(NOW, 10.0))
    with pytest.raises(RuntimeClockDiscontinuity, match="MONOTONIC_CLOCK_ROLLBACK"):
        monitor.observe(RuntimeClockObservation(NOW + timedelta(seconds=1), 9.0))

    divergent = RuntimeClockMonitor(
        max_observation_gap=timedelta(seconds=30),
        max_wall_monotonic_divergence=timedelta(seconds=1),
    )
    divergent.observe(RuntimeClockObservation(NOW, 0.0))
    with pytest.raises(RuntimeClockDiscontinuity, match="WALL_MONOTONIC_DIVERGENCE"):
        divergent.observe(RuntimeClockObservation(NOW + timedelta(seconds=5), 1.0))


def test_clock_guard_validates_configuration_and_builds_receipt() -> None:
    with pytest.raises(TypeError):
        ClockGuard(max_drift=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ClockGuard(max_drift=timedelta(0))
    guard = ClockGuard(max_drift=timedelta(seconds=2))
    assert guard.max_drift == timedelta(seconds=2)
    with pytest.raises(ClockValidationError):
        guard.verify(object())  # type: ignore[arg-type]
    with pytest.raises(ClockValidationError, match="Asia/Shanghai"):
        guard.verify(ClockObservation(NOW, NOW, "UTC"))
    with pytest.raises(ClockValidationError, match="drift"):
        guard.verify(ClockObservation(NOW, NOW + timedelta(seconds=3), "Asia/Shanghai"))
    receipt = guard.verify(ClockObservation(NOW, NOW + timedelta(milliseconds=250), "Asia/Shanghai"))
    assert receipt.drift_milliseconds == 250
    assert len(receipt.sha256) == 64


def test_writer_lease_acquire_validates_inputs_and_stale_generation(tmp_path: Path) -> None:
    path = tmp_path / "firmquant.sqlite3"
    with pytest.raises(ValueError):
        WriterLease.acquire(path, owner="")
    with pytest.raises(ValueError):
        WriterLease.acquire(path, owner="owner", ttl=timedelta(seconds=4))
    with pytest.raises(ValueError):
        WriterLease.acquire(tmp_path / "missing" / "db.sqlite3", owner="owner")
    with pytest.raises(ValueError, match="timezone-aware"):
        WriterLease.acquire(path, owner="owner", clock=lambda: NOW.replace(tzinfo=None))

    current = [NOW]
    lease = WriterLease.acquire(
        path,
        owner="first",
        ttl=timedelta(seconds=5),
        clock=lambda: current[0],
    )
    assert lease.active is True
    generation = lease.generation
    lease.release(remove_database_lease=False)
    assert lease.active is False
    lease.release()
    with pytest.raises(WriterLeaseBusy):
        WriterLease.acquire(
            path,
            owner="second",
            ttl=timedelta(seconds=5),
            clock=lambda: current[0],
        )
    current[0] += timedelta(seconds=6)
    successor = WriterLease.acquire(
        path,
        owner="second",
        ttl=timedelta(seconds=5),
        clock=lambda: current[0],
    )
    try:
        assert successor.generation == generation + 1
    finally:
        successor.release()


def test_writer_lock_probe_and_stored_expiry_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "firmquant.sqlite3"
    assert writer_lock_available(path) is False
    current = [NOW]
    lease = WriterLease.acquire(path, owner="probe", clock=lambda: current[0])
    try:
        assert writer_lock_available(path) is False
        with lease.database.transaction("EXCLUSIVE"):
            lease.database.write("UPDATE writer_leases SET expires_at = 'invalid' WHERE singleton_id = 1")
        with pytest.raises(WriterLeaseLost, match="expiry is invalid"):
            lease.assert_current()
        with pytest.raises(WriterLeaseLost, match="previously lost"):
            lease.assert_current()
    finally:
        lease.release()
    assert writer_lock_available(path) is True
    path.with_suffix(path.suffix + ".writer.lock").write_bytes(b"")
    assert writer_lock_available(path) is False


def test_writer_lease_guard_rejects_bad_monotonic_facts(tmp_path: Path) -> None:
    path = tmp_path / "firmquant.sqlite3"
    lease = WriterLease.acquire(path, owner="guard", ttl=timedelta(seconds=10), clock=lambda: NOW)
    try:
        with pytest.raises(TypeError):
            WriterLeaseGuard(object())  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            WriterLeaseGuard(lease, monotonic_clock=None)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            WriterLeaseGuard(lease, renew_interval=timedelta(seconds=10))
        with pytest.raises(TypeError):
            WriterLeaseGuard(
                lease,
                monotonic_clock=lambda: True,
                renew_interval=timedelta(seconds=1),
            )

        observed: list[object] = [2.0]
        guard = WriterLeaseGuard(
            lease,
            monotonic_clock=lambda: observed[0],  # type: ignore[return-value]
            renew_interval=timedelta(seconds=1),
        )
        assert guard.generation == lease.generation
        observed[0] = True
        with pytest.raises(WriterLeaseLost, match="became invalid"):
            guard.check()
        with pytest.raises(WriterLeaseLost, match="previously lost"):
            guard.check()

        rollback = [5.0]
        guard2 = WriterLeaseGuard(
            lease,
            monotonic_clock=lambda: rollback[0],
            renew_interval=timedelta(seconds=1),
        )
        rollback[0] = 4.0
        with pytest.raises(WriterLeaseLost, match="moved backwards"):
            guard2.check()
    finally:
        lease.release()
    with pytest.raises(WriterLeaseLost, match="no longer active"):
        lease.renew()


def _smoke_receipt() -> ProductionSmokeReceipt:
    return ProductionSmokeReceipt(
        firmquant_commit="f" * 40,
        uquant_commit="1" * 40,
        config_sha256="c" * 64,
        account_hash="a" * 64,
        safety_manifest_sha256="b" * 64,
        observed_at=NOW,
        read_healthy=True,
        position_count=0,
        order_count=0,
        fill_count=0,
    )


def test_production_smoke_receipt_and_store_validation(tmp_path: Path) -> None:
    receipt = _smoke_receipt()
    assert receipt.payload()["real_order_calls"] == 0
    assert len(receipt.sha256) == 64
    for changes, expected in (
        ({"firmquant_commit": "bad"}, ValueError),
        ({"observed_at": NOW.replace(tzinfo=None)}, ValueError),
        ({"read_healthy": 1}, TypeError),
        ({"position_count": True}, ValueError),
        ({"fill_count": -1}, ValueError),
        ({"real_order_calls": 1}, ValueError),
    ):
        with pytest.raises(expected):
            replace(receipt, **changes)
    with pytest.raises(TypeError):
        ProductionSmokeStore(object())  # type: ignore[arg-type]
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        store = ProductionSmokeStore(database)
        with pytest.raises(ValueError):
            store._from_payload({"schema": "wrong"})
        with pytest.raises(TypeError):
            store.append(object())  # type: ignore[arg-type]
        with database.transaction():
            assert store.append(receipt) is True
        assert store.append(receipt) is False
        assert (
            store.latest(
                firmquant_commit=receipt.firmquant_commit,
                uquant_commit=receipt.uquant_commit,
                config_sha256="d" * 64,
                account_hash=receipt.account_hash,
                safety_manifest_sha256=receipt.safety_manifest_sha256,
            )
            is None
        )
    finally:
        database.close()


def test_readonly_production_smoke_rejects_invalid_boundaries(tmp_path: Path) -> None:
    facts = execution_snapshot()
    snapshot = facts.broker_snapshot
    broker = FakeBroker(
        account=snapshot.account,
        positions=snapshot.positions,
        orders=snapshot.orders,
        fills=snapshot.fills,
        instruments=facts.instruments,
        quotes=facts.quotes,
        market_status=MarketSessionStatus.OPEN,
        clock=lambda: NOW,
    )
    database = Database.open(tmp_path / "firmquant.sqlite3")
    probe = facts.quotes[0].symbol
    kwargs = dict(
        broker=broker,
        database=database,
        probe_symbol=probe,
        firmquant_commit="f" * 40,
        uquant_commit="1" * 40,
        config_sha256="c" * 64,
        safety_manifest_sha256="b" * 64,
        clock=lambda: NOW,
    )
    try:
        with pytest.raises(TypeError):
            run_readonly_production_smoke(**{**kwargs, "broker": object()})
        with pytest.raises(TypeError):
            run_readonly_production_smoke(**{**kwargs, "database": object()})
        with pytest.raises(TypeError):
            run_readonly_production_smoke(**{**kwargs, "probe_symbol": "000001.SZ"})
        with pytest.raises(TypeError):
            run_readonly_production_smoke(**{**kwargs, "clock": None})
        broker.connect()
        with pytest.raises(ValueError, match="timezone-aware"):
            run_readonly_production_smoke(**{**kwargs, "clock": lambda: NOW.replace(tzinfo=None)})
    finally:
        broker.disconnect()
        database.close()
