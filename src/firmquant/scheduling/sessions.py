"""Durable workflow/runtime receipts layered on the append-only audit hash chain."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import cast

from firmquant.application.workflows import WorkflowOutcome
from firmquant.config import Mode
from firmquant.domain.states import RuntimeState, RuntimeStatus
from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.database import Database
from firmquant.persistence.repositories import canonical_json
from firmquant.persistence.writer_lease import WriterLease

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EVIDENCE_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
type EvidenceValue = str | bool | int


class WorkflowReceiptError(RuntimeError):
    """Base class for missing, malformed, or conflicting workflow evidence."""


class WorkflowConflict(WorkflowReceiptError):
    """A stable workflow identity was reused with contradictory evidence."""


class WorkflowAlreadyClaimed(WorkflowReceiptError):
    """A caller lost the atomic claim and must not execute the side effect."""


class WorkflowRecoveryRequired(WorkflowReceiptError):
    """An incomplete workflow must be recovered during startup reconciliation."""


class WorkflowStep(StrEnum):
    STARTUP = "STARTUP"
    POST_CLOSE_DECISION = "POST_CLOSE_DECISION"
    NEXT_DAY_EXECUTION = "NEXT_DAY_EXECUTION"
    INTRADAY = "INTRADAY"
    EOD = "EOD"


class WorkflowReceiptStatus(StrEnum):
    STARTED = "STARTED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"


_STEP_ORDER = {
    WorkflowStep.STARTUP: 0,
    WorkflowStep.POST_CLOSE_DECISION: 1,
    WorkflowStep.NEXT_DAY_EXECUTION: 2,
    WorkflowStep.INTRADAY: 3,
    WorkflowStep.EOD: 4,
}


def _digest(value: str, *, label: str = "workflow input") -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise WorkflowReceiptError(f"{label} must be lowercase SHA-256")


def _aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise WorkflowReceiptError("workflow receipt time must be timezone-aware")


def _safe_evidence(value: Mapping[str, EvidenceValue]) -> Mapping[str, EvidenceValue]:
    if not isinstance(value, Mapping):
        raise WorkflowReceiptError("workflow evidence must be a mapping")
    copied: dict[str, EvidenceValue] = {}
    for key, item in value.items():
        if not isinstance(key, str) or _EVIDENCE_KEY.fullmatch(key) is None:
            raise WorkflowReceiptError("workflow evidence key is not canonical")
        if type(item) not in {str, bool, int}:
            raise WorkflowReceiptError("workflow evidence value is not log-safe")
        if isinstance(item, str) and (
            not item
            or item != item.strip()
            or len(item) > 512
            or any(ord(character) < 32 or ord(character) == 127 for character in item)
        ):
            raise WorkflowReceiptError("workflow evidence text is not canonical")
        copied[key] = item
    return MappingProxyType(copied)


def _payload(raw: object) -> dict[str, object]:
    try:
        parsed: object = json.loads(str(raw))
    except json.JSONDecodeError as exc:
        raise WorkflowReceiptError("workflow audit payload is malformed") from exc
    if not isinstance(parsed, dict):
        raise WorkflowReceiptError("workflow audit payload root is not an object")
    return parsed


@dataclass(frozen=True, slots=True)
class StoredWorkflowState:
    step: WorkflowStep
    session: date
    input_sha256: str
    evidence: Mapping[str, EvidenceValue]
    owner_generation: int
    latest_status: WorkflowReceiptStatus
    completed_outcome: WorkflowOutcome | None

    @property
    def requires_recovery(self) -> bool:
        return self.latest_status is not WorkflowReceiptStatus.COMPLETED


@dataclass(frozen=True, slots=True)
class WorkflowRunResult[T]:
    value: T
    outcome: WorkflowOutcome
    recovered: bool


@dataclass(frozen=True, slots=True)
class WorkflowReceiptStore:
    """Use the one account writer lease for atomic workflow claims and runtime state."""

    writer_lease: WriterLease

    def __post_init__(self) -> None:
        if not isinstance(self.writer_lease, WriterLease):
            raise TypeError("workflow receipts require an active WriterLease")
        self.writer_lease.assert_current()

    @property
    def _database(self) -> Database:
        return self.writer_lease.database

    @property
    def _audit(self) -> AuditLedger:
        return AuditLedger(self._database)

    @staticmethod
    def _event_id(
        step: WorkflowStep,
        session: date,
        input_sha256: str,
        status: WorkflowReceiptStatus,
    ) -> str:
        return f"workflow:{step.value}:{session.isoformat()}:{input_sha256}:{status.value}"

    def _read_states(self) -> tuple[StoredWorkflowState, ...]:
        rows = self._database.query_all(
            "SELECT payload_json FROM audit_events WHERE category = 'WORKFLOW' ORDER BY sequence"
        )
        grouped: dict[tuple[WorkflowStep, date], list[dict[str, object]]] = {}
        for row in rows:
            item = _payload(row["payload_json"])
            try:
                step = WorkflowStep(str(item["step"]))
                session = date.fromisoformat(str(item["session"]))
                status = WorkflowReceiptStatus(str(item["status"]))
            except (KeyError, ValueError) as exc:
                raise WorkflowReceiptError("workflow audit identity is malformed") from exc
            item["status"] = status.value
            grouped.setdefault((step, session), []).append(item)
        states: list[StoredWorkflowState] = []
        for (step, session), events in grouped.items():
            started = [item for item in events if item["status"] == WorkflowReceiptStatus.STARTED]
            if len(started) != 1:
                raise WorkflowReceiptError("workflow must contain exactly one STARTED receipt")
            origin = started[0]
            observed_statuses = tuple(WorkflowReceiptStatus(str(item["status"])) for item in events)
            if observed_statuses not in {
                (WorkflowReceiptStatus.STARTED,),
                (WorkflowReceiptStatus.STARTED, WorkflowReceiptStatus.FAILED),
                (WorkflowReceiptStatus.STARTED, WorkflowReceiptStatus.COMPLETED),
                (
                    WorkflowReceiptStatus.STARTED,
                    WorkflowReceiptStatus.FAILED,
                    WorkflowReceiptStatus.COMPLETED,
                ),
            }:
                raise WorkflowReceiptError("workflow receipt sequence is illegal")
            try:
                input_sha256 = str(origin["input_sha256"])
                _digest(input_sha256)
                owner_hash = str(origin["owner_hash"])
                _digest(owner_hash, label="workflow owner hash")
                evidence_raw = origin["evidence"]
                if not isinstance(evidence_raw, dict):
                    raise TypeError
                evidence = _safe_evidence(cast(Mapping[str, EvidenceValue], evidence_raw))
                generation_raw = origin["owner_generation"]
                if (
                    isinstance(generation_raw, bool)
                    or not isinstance(generation_raw, int)
                    or generation_raw <= 0
                ):
                    raise ValueError
                owner_generation = generation_raw
            except (KeyError, TypeError, ValueError) as exc:
                raise WorkflowReceiptError("workflow STARTED evidence is malformed") from exc
            for event in events:
                if event.get("input_sha256") != input_sha256:
                    raise WorkflowConflict("workflow receipts disagree on input digest")
            try:
                latest_status = WorkflowReceiptStatus(str(events[-1]["status"]))
            except ValueError as exc:
                raise WorkflowReceiptError("workflow latest status is malformed") from exc
            completed_events = [item for item in events if item["status"] == WorkflowReceiptStatus.COMPLETED]
            if len(completed_events) > 1:
                raise WorkflowReceiptError("workflow contains duplicate completion receipts")
            outcome: WorkflowOutcome | None = None
            if completed_events:
                if completed_events[0] is not events[-1]:
                    raise WorkflowReceiptError("workflow has events after durable completion")
                try:
                    outcome = WorkflowOutcome(
                        output_sha256=str(completed_events[0]["output_sha256"]),
                        reference_id=str(completed_events[0]["reference_id"]),
                    )
                except (KeyError, ValueError) as exc:
                    raise WorkflowReceiptError("workflow completion evidence is malformed") from exc
                latest_status = WorkflowReceiptStatus.COMPLETED
            states.append(
                StoredWorkflowState(
                    step=step,
                    session=session,
                    input_sha256=input_sha256,
                    evidence=evidence,
                    owner_generation=owner_generation,
                    latest_status=latest_status,
                    completed_outcome=outcome,
                )
            )
        return tuple(sorted(states, key=lambda item: (item.session, _STEP_ORDER[item.step])))

    def states(self) -> tuple[StoredWorkflowState, ...]:
        self.writer_lease.assert_current()
        self._audit.verify()
        return self._read_states()

    def inspect(self, *, step: WorkflowStep, session: date) -> StoredWorkflowState | None:
        return next(
            (item for item in self.states() if item.step is step and item.session == session),
            None,
        )

    def unresolved(self) -> tuple[StoredWorkflowState, ...]:
        return tuple(item for item in self.states() if item.requires_recovery)

    def claim(
        self,
        *,
        step: WorkflowStep,
        session: date,
        input_sha256: str,
        evidence: Mapping[str, EvidenceValue],
        created_at: datetime,
    ) -> StoredWorkflowState:
        self.writer_lease.assert_current()
        _digest(input_sha256)
        _aware(created_at)
        safe_evidence = _safe_evidence(evidence)
        self._audit.verify()
        with self._database.transaction("IMMEDIATE"):
            existing = next(
                (item for item in self._read_states() if item.step is step and item.session == session),
                None,
            )
            if existing is not None:
                if existing.input_sha256 != input_sha256:
                    raise WorkflowConflict("workflow already has different sealed inputs")
                raise WorkflowAlreadyClaimed("workflow side effect was already atomically claimed")
            owner_hash = hashlib.sha256(self.writer_lease.owner.encode("utf-8")).hexdigest()
            self._audit.append(
                audit_event_id=self._event_id(step, session, input_sha256, WorkflowReceiptStatus.STARTED),
                category="WORKFLOW",
                actor="session-coordinator",
                payload={
                    "schema": "firmquant.workflow-receipt.v2",
                    "step": step.value,
                    "session": session.isoformat(),
                    "input_sha256": input_sha256,
                    "status": WorkflowReceiptStatus.STARTED.value,
                    "evidence": dict(safe_evidence),
                    "owner_hash": owner_hash,
                    "owner_generation": self.writer_lease.generation,
                },
                created_at=created_at,
            )
        state = self.inspect(step=step, session=session)
        if state is None:
            raise WorkflowReceiptError("atomic workflow claim was not durable")
        return state

    def _terminal(
        self,
        *,
        state: StoredWorkflowState,
        status: WorkflowReceiptStatus,
        created_at: datetime,
        outcome: WorkflowOutcome | None = None,
        reason_code: str | None = None,
    ) -> None:
        self.writer_lease.assert_current()
        _aware(created_at)
        current = self.inspect(step=state.step, session=state.session)
        if (
            current is None
            or current.input_sha256 != state.input_sha256
            or current.evidence != state.evidence
            or current.owner_generation != state.owner_generation
        ):
            raise WorkflowConflict("workflow claim disappeared or changed")
        if current.completed_outcome is not None:
            if status is WorkflowReceiptStatus.COMPLETED and outcome == current.completed_outcome:
                return
            raise WorkflowConflict("durably completed workflow cannot change")
        event_id = self._event_id(state.step, state.session, state.input_sha256, status)
        if (
            self._database.query_one("SELECT 1 FROM audit_events WHERE audit_event_id = ?", (event_id,))
            is not None
        ):
            return
        payload: dict[str, object] = {
            "schema": "firmquant.workflow-receipt.v2",
            "step": state.step.value,
            "session": state.session.isoformat(),
            "input_sha256": state.input_sha256,
            "status": status.value,
        }
        if outcome is not None:
            payload.update(
                output_sha256=outcome.output_sha256,
                reference_id=outcome.reference_id,
            )
        if reason_code is not None:
            payload["reason_code"] = reason_code
        with self._database.transaction("IMMEDIATE"):
            self._audit.append(
                audit_event_id=event_id,
                category="WORKFLOW",
                actor="session-coordinator",
                payload=payload,
                created_at=created_at,
            )

    def failed(self, *, state: StoredWorkflowState, created_at: datetime, reason_code: str) -> None:
        self._terminal(
            state=state,
            status=WorkflowReceiptStatus.FAILED,
            created_at=created_at,
            reason_code=reason_code,
        )

    def completed(
        self, *, state: StoredWorkflowState, created_at: datetime, outcome: WorkflowOutcome
    ) -> None:
        self._terminal(
            state=state,
            status=WorkflowReceiptStatus.COMPLETED,
            created_at=created_at,
            outcome=outcome,
        )

    def load_runtime(self, mode: Mode) -> RuntimeStatus:
        self.writer_lease.assert_current()
        self._audit.verify()
        row = self._database.query_one("SELECT * FROM runtime_state WHERE singleton_id = 1")
        if row is None:
            return RuntimeStatus.initial()
        if str(row["mode"]) != mode.value:
            raise WorkflowConflict("stored runtime mode differs from configured mode")
        try:
            blockers_raw: object = json.loads(str(row["blockers_json"]))
            if not isinstance(blockers_raw, list) or not all(isinstance(item, str) for item in blockers_raw):
                raise ValueError
            return RuntimeStatus(
                state=RuntimeState(str(row["state"])),
                revision=int(row["revision"]),
                reason=str(row["reason"]),
                blockers=tuple(blockers_raw),
            )
        except (TypeError, ValueError) as exc:
            raise WorkflowReceiptError("stored runtime state is malformed") from exc

    def save_runtime(
        self,
        *,
        mode: Mode,
        previous: RuntimeStatus,
        current: RuntimeStatus,
        created_at: datetime,
    ) -> None:
        self.writer_lease.assert_current()
        _aware(created_at)
        payload = {
            "schema": "firmquant.runtime-transition.v1",
            "mode": mode.value,
            "state": current.state.value,
            "revision": current.revision,
            "reason": current.reason,
            "blockers": current.blockers,
        }
        with self._database.transaction("IMMEDIATE"):
            row = self._database.query_one("SELECT * FROM runtime_state WHERE singleton_id = 1")
            if row is None:
                if previous != RuntimeStatus.initial():
                    raise WorkflowConflict("runtime state disappeared before transition")
                self._database.write(
                    """
                    INSERT INTO runtime_state(
                        singleton_id, mode, state, revision, reason, blockers_json, updated_at
                    ) VALUES (1, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        mode.value,
                        current.state.value,
                        current.revision,
                        current.reason,
                        canonical_json(current.blockers),
                        created_at.isoformat(),
                    ),
                )
            else:
                observed = RuntimeStatus(
                    state=RuntimeState(str(row["state"])),
                    revision=int(row["revision"]),
                    reason=str(row["reason"]),
                    blockers=tuple(json.loads(str(row["blockers_json"]))),
                )
                if str(row["mode"]) != mode.value or observed != previous:
                    raise WorkflowConflict("runtime transition lost compare-and-set ownership")
                cursor = self._database.write(
                    """
                    UPDATE runtime_state
                    SET state = ?, revision = ?, reason = ?, blockers_json = ?, updated_at = ?
                    WHERE singleton_id = 1 AND revision = ?
                    """,
                    (
                        current.state.value,
                        current.revision,
                        current.reason,
                        canonical_json(current.blockers),
                        created_at.isoformat(),
                        previous.revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise WorkflowConflict("runtime transition compare-and-set failed")
            if current.state is RuntimeState.HALTED:
                revoked = self._database.write(
                    "UPDATE arm_leases SET revoked_at = ?, revoke_reason = ? WHERE revoked_at IS NULL",
                    (created_at.isoformat(), "runtime entered HALTED"),
                )
                if revoked.rowcount:
                    self._audit.append(
                        audit_event_id=f"runtime-halt-arm-revoke:{mode.value}:{current.revision}",
                        category="ARM",
                        actor="session-coordinator",
                        payload={
                            "schema": "firmquant.arm-operation.v1",
                            "action": "REVOKE_ON_HALT",
                            "mode": mode.value,
                            "runtime_revision": current.revision,
                            "revoked_lease_count": revoked.rowcount,
                        },
                        created_at=created_at,
                    )
            self._audit.append(
                audit_event_id=f"runtime:{mode.value}:{current.revision}",
                category="RUNTIME",
                actor="session-coordinator",
                payload=payload,
                created_at=created_at,
            )


class WorkflowRunner:
    """Atomically execute a new step or explicitly reconcile an existing step."""

    def __init__(self, *, receipts: WorkflowReceiptStore) -> None:
        self._receipts = receipts

    def run_new[T](
        self,
        *,
        step: WorkflowStep,
        session: date,
        input_sha256: str,
        evidence: Mapping[str, EvidenceValue],
        now: Callable[[], datetime],
        action: Callable[[], T],
        seal: Callable[[T], WorkflowOutcome],
    ) -> WorkflowRunResult[T]:
        state = self._receipts.claim(
            step=step,
            session=session,
            input_sha256=input_sha256,
            evidence=evidence,
            created_at=now(),
        )
        try:
            value = action()
            outcome = seal(value)
            self._receipts.completed(state=state, created_at=now(), outcome=outcome)
        except Exception:
            self._receipts.failed(
                state=state,
                created_at=now(),
                reason_code="STEP_EXCEPTION",
            )
            raise
        return WorkflowRunResult(value=value, outcome=outcome, recovered=False)

    def recover_existing[T](
        self,
        *,
        state: StoredWorkflowState,
        now: Callable[[], datetime],
        recover: Callable[[str], T],
        seal: Callable[[T], WorkflowOutcome],
        allow_incomplete: bool,
    ) -> WorkflowRunResult[T]:
        if state.requires_recovery and not allow_incomplete:
            raise WorkflowRecoveryRequired("incomplete workflow requires startup recovery")
        value = recover(state.input_sha256)
        outcome = seal(value)
        if state.completed_outcome is not None and outcome != state.completed_outcome:
            raise WorkflowConflict("recovered workflow outcome differs from durable completion")
        if state.completed_outcome is None:
            self._receipts.completed(state=state, created_at=now(), outcome=outcome)
        return WorkflowRunResult(value=value, outcome=outcome, recovered=True)

    def run[T](
        self,
        *,
        step: WorkflowStep,
        session: date,
        input_sha256: str,
        now: Callable[[], datetime],
        action: Callable[[], T],
        recover: Callable[[str], T],
        seal: Callable[[T], WorkflowOutcome],
        evidence: Mapping[str, EvidenceValue] = MappingProxyType({}),
        allow_incomplete_recovery: bool = False,
    ) -> WorkflowRunResult[T]:
        state = self._receipts.inspect(step=step, session=session)
        if state is None:
            return self.run_new(
                step=step,
                session=session,
                input_sha256=input_sha256,
                evidence=evidence,
                now=now,
                action=action,
                seal=seal,
            )
        if state.input_sha256 != input_sha256:
            raise WorkflowConflict("workflow already has different sealed inputs")
        return self.recover_existing(
            state=state,
            now=now,
            recover=recover,
            seal=seal,
            allow_incomplete=allow_incomplete_recovery,
        )


__all__ = (
    "EvidenceValue",
    "StoredWorkflowState",
    "WorkflowAlreadyClaimed",
    "WorkflowConflict",
    "WorkflowReceiptError",
    "WorkflowReceiptStatus",
    "WorkflowReceiptStore",
    "WorkflowRecoveryRequired",
    "WorkflowRunResult",
    "WorkflowRunner",
    "WorkflowStep",
)
