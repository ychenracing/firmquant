"""Single-writer execution of local risk-reducing production control requests."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime

from firmquant.application.control_channel import ControlCommand, ControlExecution, ControlRequest
from firmquant.broker.gateway import BrokerGateway
from firmquant.config import Mode
from firmquant.domain.states import RuntimeState, RuntimeStatus
from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.production_repository import MonotonicExecutionLedgerRepository
from firmquant.persistence.repositories import canonical_json
from firmquant.persistence.writer_lease import WriterLease
from firmquant.risk.cancel_only import CancelOnlyCapabilityFactory, CancelOnlyResult
from firmquant.scheduling.sessions import WorkflowReceiptStore


class RuntimeControlError(RuntimeError):
    """A local control request could not be applied safely by the active writer."""


def _aware(value: datetime, *, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{label} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


class RuntimeControlExecutor:
    """Execute only risk-reducing controls while retaining one serialized state writer."""

    def __init__(
        self,
        *,
        mode: Mode,
        writer: WriterLease,
        broker: BrokerGateway | None,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(mode, Mode):
            raise TypeError("runtime control mode must be Mode")
        if not isinstance(writer, WriterLease):
            raise TypeError("runtime control requires WriterLease")
        if broker is not None and not isinstance(broker, BrokerGateway):
            raise TypeError("runtime control broker must satisfy BrokerGateway")
        if not callable(clock):
            raise TypeError("runtime control clock must be callable")
        self._mode = mode
        self._writer = writer
        self._broker = broker
        self._clock = clock
        self._cancel_calls = 0
        self._stop_pending = False

    @property
    def cancel_calls(self) -> int:
        return self._cancel_calls

    @property
    def stop_pending(self) -> bool:
        return self._stop_pending

    def execute(self, request: ControlRequest) -> ControlExecution:
        if not isinstance(request, ControlRequest):
            raise TypeError("runtime control request must be typed")
        self._writer.assert_current()
        if request.command is ControlCommand.HALT:
            return self._halt(request_id=request.request_id, reason_sha256=request.reason_sha256)
        if request.command is ControlCommand.DISARM:
            return self._disarm(request_id=request.request_id, reason_sha256=request.reason_sha256)
        if request.command is ControlCommand.CANCEL_SYSTEM_ORDERS:
            return self._cancel(request_id=request.request_id, reason_sha256=request.reason_sha256)
        if request.command is ControlCommand.STOP:
            return self._stop(request_id=request.request_id, reason_sha256=request.reason_sha256)
        raise RuntimeControlError("CONTROL_COMMAND_UNSUPPORTED")

    def execute_internal_stop(self, *, source: str = "PROCESS_SIGNAL") -> ControlExecution:
        """Translate a signal flag into the same serialized STOP state semantics."""

        if not isinstance(source, str) or not source or source != source.strip():
            raise ValueError("internal stop source must be canonical text")
        request_id = (
            "internal-stop-" + hashlib.sha256(f"{source}:{self._writer.generation}".encode()).hexdigest()
        )
        return self._stop(request_id=request_id, reason_sha256=None)

    def finalize_stop(self) -> RuntimeStatus:
        """Persist clean DISARMED completion only after broker disconnect has finished."""

        self._writer.assert_current()
        receipts = WorkflowReceiptStore(writer_lease=self._writer)
        status = receipts.load_runtime(self._mode)
        if status.state is RuntimeState.STOPPING:
            status = self._save_transition(
                receipts,
                status=status,
                target=RuntimeState.DISARMED,
                reason="production stop completed",
                blockers=("KILL_SWITCH",) if "KILL_SWITCH" in status.blockers else (),
            )
        self._stop_pending = False
        return status

    def _halt(self, *, request_id: str, reason_sha256: str | None) -> ControlExecution:
        now = self._now()
        revoked = self._revoke_arm(now=now, reason="local control halt")
        receipts = WorkflowReceiptStore(writer_lease=self._writer)
        status = receipts.load_runtime(self._mode)
        if status.state is RuntimeState.STOPPING:
            status = self._save_transition(
                receipts,
                status=status,
                target=RuntimeState.DISARMED,
                reason="stop completed before emergency halt",
                blockers=(),
            )
        if status.state is RuntimeState.DISARMED:
            status = self._save_transition(
                receipts,
                status=status,
                target=RuntimeState.STARTING,
                reason="local emergency halt control",
                blockers=(),
            )
        status = self._save_transition(
            receipts,
            status=status,
            target=RuntimeState.HALTED,
            reason="local emergency halt",
            blockers=tuple(sorted(set(status.blockers) | {"KILL_SWITCH"})),
        )
        self._append_halt_evidence(
            request_id=request_id,
            reason_sha256=reason_sha256,
            status=status,
            now=now,
        )
        return ControlExecution(
            outcome={
                "armed": False,
                "auto_liquidation": False,
                "command": ControlCommand.HALT.value,
                "revoked_lease_count": revoked,
                "runtime_state": status.state.value,
            },
            halted=True,
        )

    def _disarm(self, *, request_id: str, reason_sha256: str | None) -> ControlExecution:
        now = self._now()
        revoked = self._revoke_arm(now=now, reason="local control disarm")
        receipts = WorkflowReceiptStore(writer_lease=self._writer)
        status = receipts.load_runtime(self._mode)
        if status.state is not RuntimeState.DISARMED:
            if status.state is not RuntimeState.STOPPING:
                status = self._save_transition(
                    receipts,
                    status=status,
                    target=RuntimeState.STOPPING,
                    reason="local disarm requested",
                    blockers=status.blockers,
                )
            status = self._save_transition(
                receipts,
                status=status,
                target=RuntimeState.DISARMED,
                reason="local disarm completed",
                blockers=("KILL_SWITCH",) if "KILL_SWITCH" in status.blockers else (),
            )
        self._append_audit(
            request_id=request_id,
            command=ControlCommand.DISARM,
            reason_sha256=reason_sha256,
            payload={"revoked_lease_count": revoked, "runtime_state": status.state.value},
            now=now,
        )
        return ControlExecution(
            outcome={
                "armed": False,
                "command": ControlCommand.DISARM.value,
                "revoked_lease_count": revoked,
                "runtime_state": status.state.value,
            },
            halted=True,
        )

    def _cancel(self, *, request_id: str, reason_sha256: str | None) -> ControlExecution:
        now = self._now()
        if self._mode not in {Mode.CANARY, Mode.LIVE}:
            result = CancelOnlyResult(mode_write_forbidden=True)
        elif self._broker is None:
            raise RuntimeControlError("CANCEL_BROKER_SESSION_UNAVAILABLE")
        else:
            capability = CancelOnlyCapabilityFactory(mode=self._mode).create(
                gateway=self._broker,
                ledger=MonotonicExecutionLedgerRepository(self._writer.database),
                clock=self._clock,
            )
            result = capability.cancel_system_orders()
            self._cancel_calls += result.cancel_calls
        payload = {
            "cancel_calls": result.cancel_calls,
            "cancelled_order_ids": list(result.cancelled_order_ids),
            "command": ControlCommand.CANCEL_SYSTEM_ORDERS.value,
            "denied_order_ids": list(result.denied_order_ids),
            "mode_write_forbidden": result.mode_write_forbidden,
            "terminal_order_ids": list(result.terminal_order_ids),
            "unknown_order_ids": list(result.unknown_order_ids),
        }
        self._append_audit(
            request_id=request_id,
            command=ControlCommand.CANCEL_SYSTEM_ORDERS,
            reason_sha256=reason_sha256,
            payload=payload,
            now=now,
        )
        return ControlExecution(outcome=payload)

    def _stop(self, *, request_id: str, reason_sha256: str | None) -> ControlExecution:
        now = self._now()
        revoked = self._revoke_arm(now=now, reason="local control stop")
        receipts = WorkflowReceiptStore(writer_lease=self._writer)
        status = receipts.load_runtime(self._mode)
        if status.state is RuntimeState.DISARMED:
            status = self._save_transition(
                receipts,
                status=status,
                target=RuntimeState.STARTING,
                reason="local stop control",
                blockers=(),
            )
        if status.state is not RuntimeState.STOPPING:
            status = self._save_transition(
                receipts,
                status=status,
                target=RuntimeState.STOPPING,
                reason="local stop requested",
                blockers=status.blockers,
            )
        self._stop_pending = True
        self._append_audit(
            request_id=request_id,
            command=ControlCommand.STOP,
            reason_sha256=reason_sha256,
            payload={"revoked_lease_count": revoked, "runtime_state": status.state.value},
            now=now,
        )
        return ControlExecution(
            outcome={
                "armed": False,
                "command": ControlCommand.STOP.value,
                "revoked_lease_count": revoked,
                "runtime_state": status.state.value,
            },
            halted=True,
            stop=True,
        )

    def _save_transition(
        self,
        receipts: WorkflowReceiptStore,
        *,
        status: RuntimeStatus,
        target: RuntimeState,
        reason: str,
        blockers: tuple[str, ...],
    ) -> RuntimeStatus:
        current = status.transition(target, reason=reason, blockers=blockers)
        if current != status:
            receipts.save_runtime(
                mode=self._mode,
                previous=status,
                current=current,
                created_at=self._now(),
            )
        return current

    def _revoke_arm(self, *, now: datetime, reason: str) -> int:
        self._writer.assert_current()
        with self._writer.database.transaction():
            cursor = self._writer.database.write(
                "UPDATE arm_leases SET revoked_at = ?, revoke_reason = ? WHERE revoked_at IS NULL",
                (now.isoformat(), reason),
            )
        return int(cursor.rowcount)

    def _append_halt_evidence(
        self,
        *,
        request_id: str,
        reason_sha256: str | None,
        status: RuntimeStatus,
        now: datetime,
    ) -> None:
        event_id = "control-halt:" + hashlib.sha256(request_id.encode()).hexdigest()
        payload = {
            "schema": "firmquant.kill-switch.v1",
            "control_request_id_sha256": hashlib.sha256(request_id.encode()).hexdigest(),
            "reason_sha256": reason_sha256 or hashlib.sha256(b"local emergency halt").hexdigest(),
        }
        payload_json = canonical_json(payload)
        with self._writer.database.transaction():
            if (
                self._writer.database.query_one(
                    "SELECT 1 FROM risk_events WHERE risk_event_id = ?", (event_id,)
                )
                is None
            ):
                self._writer.database.write(
                    """
                    INSERT INTO risk_events(
                        risk_event_id, severity, code, execution_id, symbol,
                        payload_json, payload_sha256, created_at
                    ) VALUES (?, 'CRITICAL', 'KILL_SWITCH_TRIPPED', NULL, NULL, ?, ?, ?)
                    """,
                    (
                        event_id,
                        payload_json,
                        hashlib.sha256(payload_json.encode()).hexdigest(),
                        now.isoformat(),
                    ),
                )
        self._append_audit(
            request_id=request_id,
            command=ControlCommand.HALT,
            reason_sha256=reason_sha256,
            payload={"runtime_state": status.state.value, "auto_liquidation": False},
            now=now,
        )

    def _append_audit(
        self,
        *,
        request_id: str,
        command: ControlCommand,
        reason_sha256: str | None,
        payload: dict[str, object],
        now: datetime,
    ) -> None:
        request_hash = hashlib.sha256(request_id.encode()).hexdigest()
        event_id = f"control:{command.value.lower()}:{request_hash}"
        with self._writer.database.transaction():
            if (
                self._writer.database.query_one(
                    "SELECT 1 FROM audit_events WHERE audit_event_id = ?", (event_id,)
                )
                is not None
            ):
                return
            AuditLedger(self._writer.database).append(
                audit_event_id=event_id,
                category="RUNTIME_CONTROL",
                actor="local-control",
                payload={
                    "schema": "firmquant.runtime-control.v1",
                    "command": command.value,
                    "request_id_sha256": request_hash,
                    "reason_sha256": reason_sha256,
                    **payload,
                },
                created_at=now,
            )

    def _now(self) -> datetime:
        return _aware(self._clock(), label="runtime control clock")


__all__ = ("RuntimeControlError", "RuntimeControlExecutor")
