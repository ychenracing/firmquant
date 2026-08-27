from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from firmquant.application.control_channel import ControlCommand, ControlRequest
from firmquant.application.runtime_control import RuntimeControlError, RuntimeControlExecutor
from firmquant.broker.fake import BrokerOperation, ScriptedOutcome
from firmquant.config import Mode
from firmquant.domain.broker_facts import BrokerOrderStatus
from firmquant.domain.orders import OrderState
from firmquant.domain.states import RuntimeState, RuntimeStatus
from firmquant.persistence.account_authority import AccountBinding, AccountBindingRepository
from firmquant.persistence.writer_lease import WriterLease
from firmquant.scheduling.sessions import WorkflowReceiptStore
from tests.fixtures.recovery_cases import (
    NOW,
    acknowledge_locally,
    broker_order,
    create_submitting_case,
    fake_recovery_broker,
)


def _request(command: ControlCommand, marker: str) -> ControlRequest:
    return ControlRequest(
        request_id="ctrl_" + marker * 64,
        command=command,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=1),
        host_hash="f" * 64,
        reason_sha256="e" * 64,
    )


def _transition_runtime(
    writer: WriterLease,
    mode: Mode,
    targets: tuple[RuntimeState, ...],
) -> RuntimeStatus:
    receipts = WorkflowReceiptStore(writer_lease=writer)
    status = receipts.load_runtime(mode)
    for target in targets:
        current = status.transition(
            target,
            reason=f"test transition to {target.value}",
            blockers=("KILL_SWITCH",) if target is RuntimeState.HALTED else (),
        )
        receipts.save_runtime(
            mode=mode,
            previous=status,
            current=current,
            created_at=NOW,
        )
        status = current
    return status


def _seed_arm(writer: WriterLease, *, expired: bool = False) -> None:
    issued = NOW - timedelta(minutes=10) if expired else NOW
    expires = NOW - timedelta(minutes=5) if expired else NOW + timedelta(minutes=5)
    with writer.database.transaction():
        writer.database.write(
            """
            INSERT INTO arm_leases(
                lease_id, mode, host_hash, account_hash, firmquant_commit,
                uquant_commit, config_sha256, identity_payload_sha256,
                issued_at, expires_at, revoked_at, revoke_reason, lease_mac
            ) VALUES (?, 'CANARY', ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
            """,
            (
                "arm_" + "a" * 32,
                writer.host_hash,
                "b" * 64,
                "c" * 40,
                "d" * 40,
                "e" * 64,
                "f" * 64,
                issued.isoformat(),
                expires.isoformat(),
                "0" * 64,
            ),
        )


def _bind_account(writer: WriterLease, account_hash: str) -> None:
    broker = fake_recovery_broker()
    binding = AccountBinding.create(
        account_id_hash=account_hash,
        account_type=broker.query_account().account_type,
        broker_snapshot_sha256="b" * 64,
        account_state_sha256="c" * 64,
        uquant_commit="1" * 40,
        uquant_code_fingerprint="d" * 64,
        data_hash="e" * 64,
        data_as_of="2026-08-25",
        data_symbols=("600519.SH",),
        created_at=NOW,
    )
    AccountBindingRepository(writer.database).bind(binding)


def test_disarm_revokes_arm_and_persists_disarmed_without_broker_write(tmp_path: Path) -> None:
    with WriterLease.acquire(tmp_path / "firmquant.sqlite3", owner="runtime-control") as writer:
        _transition_runtime(
            writer,
            Mode.CANARY,
            (RuntimeState.STARTING, RuntimeState.RECONCILING, RuntimeState.READY),
        )
        _seed_arm(writer)
        executor = RuntimeControlExecutor(
            mode=Mode.CANARY,
            writer=writer,
            broker=None,
            clock=lambda: NOW,
        )

        result = executor.execute(_request(ControlCommand.DISARM, "1"))

        assert result.halted is True
        assert result.stop is False
        assert result.outcome["armed"] is False
        assert result.outcome["revoked_lease_count"] == 1
        assert writer.database.scalar("SELECT state FROM runtime_state WHERE singleton_id = 1") == "DISARMED"
        assert writer.database.scalar("SELECT count(*) FROM arm_leases WHERE revoked_at IS NOT NULL") == 1
        assert executor.cancel_calls == 0


def test_halted_runtime_with_expired_arm_can_still_cancel_system_order(tmp_path: Path) -> None:
    with WriterLease.acquire(tmp_path / "firmquant.sqlite3", owner="runtime-control") as writer:
        case = create_submitting_case(writer.database)
        acknowledged_fact = broker_order(case.command)
        acknowledged = acknowledge_locally(case, acknowledged_fact)
        broker = fake_recovery_broker(orders=(acknowledged_fact,))
        _bind_account(writer, broker.query_account().account_id_hash)
        _transition_runtime(writer, Mode.CANARY, (RuntimeState.STARTING, RuntimeState.HALTED))
        _seed_arm(writer, expired=True)
        cancelled = replace(
            acknowledged_fact,
            status=BrokerOrderStatus.CANCELLED,
            event_sequence=acknowledged_fact.event_sequence + 1,
        )
        broker.script((ScriptedOutcome(BrokerOperation.CANCEL, response=cancelled),))
        executor = RuntimeControlExecutor(
            mode=Mode.CANARY,
            writer=writer,
            broker=broker,
            clock=lambda: NOW,
        )

        result = executor.execute(_request(ControlCommand.CANCEL_SYSTEM_ORDERS, "2"))

        assert result.outcome["cancelled_order_ids"] == [acknowledged.broker_order_id]
        assert result.outcome["cancel_calls"] == 1
        assert executor.cancel_calls == 1
        assert broker.cancelled_order_ids == (acknowledged.broker_order_id,)
        current = case.repository.load(case.aggregate.intent.execution_id)
        assert current is not None and current.state is OrderState.CANCELLED
        assert writer.database.scalar("SELECT state FROM runtime_state WHERE singleton_id = 1") == "HALTED"


def test_internal_signal_stop_is_serialized_and_finalized_after_disconnect_boundary(tmp_path: Path) -> None:
    with WriterLease.acquire(tmp_path / "firmquant.sqlite3", owner="runtime-control") as writer:
        _transition_runtime(
            writer,
            Mode.SHADOW,
            (RuntimeState.STARTING, RuntimeState.RECONCILING, RuntimeState.READY),
        )
        executor = RuntimeControlExecutor(
            mode=Mode.SHADOW,
            writer=writer,
            broker=None,
            clock=lambda: NOW,
        )

        result = executor.execute_internal_stop(source="SIGTERM")
        assert result.stop is True
        assert result.halted is True
        assert executor.stop_pending is True
        assert writer.database.scalar("SELECT state FROM runtime_state WHERE singleton_id = 1") == "STOPPING"

        final = executor.finalize_stop()
        assert final.state is RuntimeState.DISARMED
        assert executor.stop_pending is False
        assert executor.finalize_stop().state is RuntimeState.DISARMED


@pytest.mark.parametrize("mode", [Mode.PAPER, Mode.REPLAY, Mode.SHADOW])
def test_cancel_control_is_structurally_write_free_outside_canary_live(tmp_path: Path, mode: Mode) -> None:
    with WriterLease.acquire(tmp_path / f"{mode.value.lower()}.sqlite3", owner="runtime-control") as writer:
        executor = RuntimeControlExecutor(
            mode=mode,
            writer=writer,
            broker=None,
            clock=lambda: NOW,
        )
        result = executor.execute(_request(ControlCommand.CANCEL_SYSTEM_ORDERS, "3"))

        assert result.outcome["mode_write_forbidden"] is True
        assert result.outcome["cancel_calls"] == 0
        assert result.outcome["cancelled_order_ids"] == []
        assert executor.cancel_calls == 0


def test_live_cancel_requires_attached_broker_session(tmp_path: Path) -> None:
    with WriterLease.acquire(tmp_path / "firmquant.sqlite3", owner="runtime-control") as writer:
        executor = RuntimeControlExecutor(
            mode=Mode.LIVE,
            writer=writer,
            broker=None,
            clock=lambda: NOW,
        )
        with pytest.raises(RuntimeControlError, match="CANCEL_BROKER_SESSION_UNAVAILABLE"):
            executor.execute(_request(ControlCommand.CANCEL_SYSTEM_ORDERS, "4"))


def test_halt_is_durable_idempotent_evidence_and_never_liquidates(tmp_path: Path) -> None:
    with WriterLease.acquire(tmp_path / "firmquant.sqlite3", owner="runtime-control") as writer:
        _seed_arm(writer)
        executor = RuntimeControlExecutor(
            mode=Mode.CANARY,
            writer=writer,
            broker=None,
            clock=lambda: NOW,
        )
        request = _request(ControlCommand.HALT, "5")

        first = executor.execute(request)
        second = executor.execute(request)

        assert first.outcome["auto_liquidation"] is False
        assert second.outcome["auto_liquidation"] is False
        assert writer.database.scalar("SELECT state FROM runtime_state WHERE singleton_id = 1") == "HALTED"
        assert writer.database.scalar("SELECT count(*) FROM risk_events WHERE code = 'KILL_SWITCH_TRIPPED'") == 1
        assert writer.database.scalar("SELECT count(*) FROM audit_events WHERE category = 'RUNTIME_CONTROL'") == 1
        assert executor.cancel_calls == 0


def test_runtime_control_rejects_invalid_construction_and_input(tmp_path: Path) -> None:
    with WriterLease.acquire(tmp_path / "firmquant.sqlite3", owner="runtime-control") as writer:
        with pytest.raises(TypeError, match="mode"):
            RuntimeControlExecutor(mode="LIVE", writer=writer, broker=None, clock=lambda: NOW)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="clock"):
            RuntimeControlExecutor(mode=Mode.LIVE, writer=writer, broker=None, clock=None)  # type: ignore[arg-type]
        executor = RuntimeControlExecutor(
            mode=Mode.LIVE,
            writer=writer,
            broker=None,
            clock=lambda: datetime(2026, 8, 25, 1, 32),
        )
        with pytest.raises(ValueError, match="timezone-aware"):
            executor.execute_internal_stop()
