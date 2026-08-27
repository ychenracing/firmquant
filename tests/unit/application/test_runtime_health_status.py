from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from firmquant.config import Mode
from firmquant.domain.states import RuntimeState
from firmquant.persistence.writer_lease import WriterLease
from firmquant.scheduling.sessions import WorkflowReceiptStore
from tests.integration.test_cli_operations import (
    NOW,
    initialize,
    interaction,
    live_config,
    request,
    service,
)
from firmquant.application.operations import OperatorCommand


def _ready_runtime_and_heartbeat(config: Path, *, observed_age: timedelta | None) -> None:
    database_path = config.parent / "state" / "firmquant.sqlite3"
    with WriterLease.acquire(
        database_path,
        owner="runtime-health-status-test",
        clock=lambda: NOW,
    ) as writer:
        receipts = WorkflowReceiptStore(writer_lease=writer)
        status = receipts.load_runtime(Mode.CANARY)
        for target, reason in (
            (RuntimeState.STARTING, "test start"),
            (RuntimeState.RECONCILING, "test reconcile"),
            (RuntimeState.READY, "test ready"),
        ):
            current = status.transition(target, reason=reason)
            receipts.save_runtime(
                mode=Mode.CANARY,
                previous=status,
                current=current,
                created_at=NOW,
            )
            status = current
        if observed_age is None:
            return
        observed_at = NOW - observed_age
        writer.database.write(
            """
            INSERT INTO production_heartbeat(
                singleton_id, mode, runtime_state, observed_at, host_hash, process_id,
                writer_generation, broker_connected, broker_read_healthy,
                broker_write_healthy, pending_events, last_broker_event, last_quote,
                last_reconciliation, last_decision, last_execution,
                control_request_state, processed_events, decisions, executions, eod
            ) VALUES (1, ?, ?, ?, ?, ?, ?, 1, 1, 1, 0, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0)
            """,
            (
                Mode.CANARY.value,
                RuntimeState.READY.value,
                observed_at.isoformat(),
                "h" * 64,
                12345,
                writer.generation,
                (NOW - timedelta(seconds=10)).isoformat(),
                (NOW - timedelta(seconds=9)).isoformat(),
                (NOW - timedelta(seconds=8)).isoformat(),
                (NOW - timedelta(seconds=7)).isoformat(),
                (NOW - timedelta(seconds=6)).isoformat(),
                "IDLE",
            ),
        )


def _configured_service(tmp_path: Path):
    config = tmp_path / "firmquant.toml"
    live_config(config)
    operator = service(config)
    initialize(operator)
    return config, operator


def test_fresh_heartbeat_preserves_ready_process_health(tmp_path: Path) -> None:
    config, operator = _configured_service(tmp_path)
    _ready_runtime_and_heartbeat(config, observed_age=timedelta(seconds=5))

    result = operator.execute(request(OperatorCommand.STATUS), interaction())

    assert result.payload["stored_runtime_state"] == RuntimeState.READY.value
    assert result.payload["runtime_state"] == RuntimeState.READY.value
    assert result.payload["process_health"] == "HEALTHY"
    assert result.payload["heartbeat_age"] == 5.0
    assert result.payload["broker_connection"] == "CONNECTED"
    assert "HEARTBEAT_STALE" not in result.payload["blockers"]


def test_stale_heartbeat_never_reports_old_ready_as_healthy(tmp_path: Path) -> None:
    config, operator = _configured_service(tmp_path)
    _ready_runtime_and_heartbeat(config, observed_age=timedelta(seconds=31))

    result = operator.execute(request(OperatorCommand.STATUS), interaction())

    assert result.payload["stored_runtime_state"] == RuntimeState.READY.value
    assert result.payload["runtime_state"] == RuntimeState.HALTED.value
    assert result.payload["process_health"] == "STALE"
    assert result.payload["heartbeat_age"] == 31.0
    assert "HEARTBEAT_STALE" in result.payload["blockers"]


def test_missing_heartbeat_reports_not_running_even_with_old_ready_state(tmp_path: Path) -> None:
    config, operator = _configured_service(tmp_path)
    _ready_runtime_and_heartbeat(config, observed_age=None)

    result = operator.execute(request(OperatorCommand.STATUS), interaction())

    assert result.payload["stored_runtime_state"] == RuntimeState.READY.value
    assert result.payload["runtime_state"] == RuntimeState.HALTED.value
    assert result.payload["process_health"] == "NOT_RUNNING"
    assert result.payload["heartbeat_age"] is None
    assert "PROCESS_NOT_RUNNING" in result.payload["blockers"]
