"""Mode-specific process composition with a capability-safe SHADOW runtime."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Never, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from firmquant.broker.gateway import (
    BrokerEventSink,
    BrokerGateway,
    BrokerHealth,
)
from firmquant.config import Mode
from firmquant.domain.broker_facts import (
    BrokerAccountFact,
    BrokerFillFact,
    BrokerOrderFact,
    BrokerPositionFact,
    BrokerSnapshot,
    InstrumentFact,
    MarketSessionStatus,
    QuoteFact,
)
from firmquant.domain.errors import DomainTypeError, DomainValidationError
from firmquant.domain.states import RuntimeState, RuntimeStatus
from firmquant.domain.values import Symbol
from firmquant.execution.planner import ExecutionPlan
from firmquant.persistence.repositories import canonical_json, canonical_sha256
from firmquant.strategy.snapshots import DecisionSnapshot

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class RuntimeErrorBase(RuntimeError):
    """Base error for explicit runtime orchestration failures."""


class ModeCompositionError(RuntimeErrorBase):
    """The requested mode is not available through this capability composition."""


class ModeWriteBlocked(RuntimeErrorBase):
    """A write operation was requested from a structurally read-only mode."""


class BrokerSnapshotUnstable(RuntimeErrorBase):
    """Two bounded broker reads disagreed, so no complete snapshot was asserted."""


class StartupReconciliationFailed(RuntimeErrorBase):
    """Startup evidence did not permit READY."""


class RuntimeSessionError(RuntimeErrorBase):
    """A SHADOW session failed and the process retained a HALTED state."""


def _aware(value: datetime, *, label: str) -> None:
    if not isinstance(value, datetime):
        raise DomainTypeError(f"{label} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainValidationError(f"{label} must be timezone-aware")


def _text(value: str, *, label: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise DomainValidationError(f"{label} must be canonical non-empty text")


def _digest(value: str, *, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise DomainValidationError(f"{label} must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class ShadowReconciliation:
    """Minimal immutable startup verdict adapted from the reconciliation service."""

    receipt_id: str
    passed: bool
    blockers: tuple[str, ...]
    broker_snapshot_sha256: str

    def __post_init__(self) -> None:
        _text(self.receipt_id, label="shadow reconciliation receipt id")
        if not isinstance(self.passed, bool):
            raise DomainTypeError("shadow reconciliation passed must be bool")
        if not isinstance(self.blockers, tuple):
            raise DomainTypeError("shadow reconciliation blockers must be tuple")
        if self.blockers != tuple(sorted(set(self.blockers))):
            raise DomainValidationError("shadow reconciliation blockers must be sorted and unique")
        for blocker in self.blockers:
            _text(blocker, label="shadow reconciliation blocker")
        if self.passed != (not self.blockers):
            raise DomainValidationError("shadow reconciliation pass result contradicts blockers")
        _digest(
            self.broker_snapshot_sha256,
            label="shadow reconciliation broker snapshot digest",
        )


@dataclass(frozen=True, slots=True)
class ShadowReportReceipt:
    """Evidence that the hypothetical plan report was durably rendered."""

    report_id: str
    payload_sha256: str

    def __post_init__(self) -> None:
        _text(self.report_id, label="shadow report id")
        _digest(self.payload_sha256, label="shadow report payload digest")


@dataclass(frozen=True, slots=True)
class ShadowSessionDraft:
    """Complete plan/report input containing no broker write capability."""

    mode: Mode
    broker_snapshot: BrokerSnapshot
    reconciliation: ShadowReconciliation
    decision: DecisionSnapshot
    plan: ExecutionPlan
    started_at: datetime
    completed_at: datetime

    def __post_init__(self) -> None:
        if self.mode is not Mode.SHADOW:
            raise DomainValidationError("shadow session draft mode must be SHADOW")
        if not isinstance(self.broker_snapshot, BrokerSnapshot):
            raise DomainTypeError("shadow draft broker snapshot must be BrokerSnapshot")
        if not isinstance(self.reconciliation, ShadowReconciliation):
            raise DomainTypeError("shadow draft reconciliation must be typed")
        if not isinstance(self.decision, DecisionSnapshot):
            raise DomainTypeError("shadow draft decision must be DecisionSnapshot")
        if not isinstance(self.plan, ExecutionPlan):
            raise DomainTypeError("shadow draft plan must be ExecutionPlan")
        _aware(self.started_at, label="shadow draft start time")
        _aware(self.completed_at, label="shadow draft completion time")
        if self.completed_at < self.started_at:
            raise DomainValidationError("shadow draft completed before it started")
        if self.plan.decision_id != self.decision.decision_id:
            raise DomainValidationError("shadow plan and decision identity differ")
        if self.reconciliation.broker_snapshot_sha256 != self.broker_snapshot.raw_payload_sha256:
            raise DomainValidationError("shadow reconciliation and broker snapshot identity differ")

    @property
    def payload_sha256(self) -> str:
        return canonical_sha256(
            {
                "schema": "firmquant.shadow-session.v1",
                "mode": self.mode,
                "broker_snapshot_sha256": self.broker_snapshot.raw_payload_sha256,
                "reconciliation_receipt_id": self.reconciliation.receipt_id,
                "decision_id": self.decision.decision_id,
                "plan_id": self.plan.plan_id,
                "planned_order_count": len(self.plan.orders),
                "planning_blockers": [blocker.reason_code for blocker in self.plan.blockers],
                "started_at": self.started_at,
                "completed_at": self.completed_at,
            }
        )


@dataclass(frozen=True, slots=True)
class ShadowStartupResult:
    mode: Mode
    runtime_state: RuntimeState
    broker_snapshot: BrokerSnapshot
    reconciliation: ShadowReconciliation


@dataclass(frozen=True, slots=True)
class ShadowSessionResult:
    mode: Mode
    runtime_state: RuntimeState
    decision_id: str
    plan_id: str
    report_id: str
    planned_order_count: int
    planning_blockers: tuple[str, ...]
    plan_created: bool
    report_created: bool
    broker_snapshot_sha256: str
    report_payload_sha256: str


@runtime_checkable
class ShadowWorkflow(Protocol):
    """Application services used by SHADOW without receiving a broker write port."""

    def reconcile_startup(self, snapshot: BrokerSnapshot) -> ShadowReconciliation: ...

    def decide_after_close(self, snapshot: BrokerSnapshot) -> DecisionSnapshot: ...

    def plan_next_session(
        self,
        decision: DecisionSnapshot,
        session: ReadOnlyBrokerSession,
        snapshot: BrokerSnapshot,
    ) -> ExecutionPlan: ...

    def write_shadow_report(self, draft: ShadowSessionDraft) -> ShadowReportReceipt: ...


@dataclass(frozen=True, slots=True)
class _BrokerRead:
    account: BrokerAccountFact
    positions: tuple[BrokerPositionFact, ...]
    orders: tuple[BrokerOrderFact, ...]
    fills: tuple[BrokerFillFact, ...]


def _read_signature(read: _BrokerRead) -> str:
    """Compare economic/status fields while excluding adapter observation timestamps."""

    return canonical_sha256(
        {
            "account": {
                "account_id_hash": read.account.account_id_hash,
                "account_type": read.account.account_type,
                "available_cash": read.account.available_cash,
                "total_assets": read.account.total_assets,
            },
            "positions": [
                {
                    "symbol": item.symbol,
                    "total_shares": item.total_shares,
                    "sellable_shares": item.sellable_shares,
                    "average_cost": item.average_cost,
                    "market_value": item.market_value,
                }
                for item in sorted(read.positions, key=lambda position: position.symbol.canonical)
            ],
            "orders": [
                {
                    "broker_order_id": item.broker_order_id,
                    "client_order_id": item.client_order_id,
                    "symbol": item.symbol,
                    "side": item.side,
                    "price_type": item.price_type,
                    "status": item.status,
                    "requested_shares": item.requested_shares,
                    "filled_shares": item.filled_shares,
                    "limit_price": item.limit_price,
                    "session_date": item.session_date,
                    "event_sequence": item.event_sequence,
                }
                for item in sorted(read.orders, key=lambda order: order.broker_order_id)
            ],
            "fills": [
                {
                    "broker_fill_id": item.broker_fill_id,
                    "broker_order_id": item.broker_order_id,
                    "symbol": item.symbol,
                    "side": item.side,
                    "status": item.status,
                    "shares": item.shares,
                    "price": item.price,
                    "commission": item.commission,
                    "stamp_duty": item.stamp_duty,
                    "transfer_fee": item.transfer_fee,
                    "session_date": item.session_date,
                    "event_sequence": item.event_sequence,
                }
                for item in sorted(read.fills, key=lambda fill: fill.broker_fill_id)
            ],
        }
    )


def _snapshot_payload(read: _BrokerRead, *, captured_at: datetime) -> dict[str, object]:
    return {
        "schema": "firmquant.broker-snapshot.v1",
        "captured_at": captured_at,
        "read_signature_sha256": _read_signature(read),
        "account": {
            "account_id_hash": read.account.account_id_hash,
            "account_type": read.account.account_type,
            "available_cash": read.account.available_cash,
            "total_assets": read.account.total_assets,
        },
        "positions": [
            {
                "symbol": item.symbol,
                "total_shares": item.total_shares,
                "sellable_shares": item.sellable_shares,
                "average_cost": item.average_cost,
                "market_value": item.market_value,
            }
            for item in sorted(read.positions, key=lambda position: position.symbol.canonical)
        ],
        "order_payload_sha256": [
            item.raw_payload_sha256 for item in sorted(read.orders, key=lambda order: order.broker_order_id)
        ],
        "fill_payload_sha256": [
            item.raw_payload_sha256 for item in sorted(read.fills, key=lambda fill: fill.broker_fill_id)
        ],
    }


class ReadOnlyBrokerSession:
    """Broker read capability whose public surface intentionally has no write methods."""

    __slots__ = ("_clock", "_gateway")

    def __init__(
        self,
        *,
        gateway: BrokerGateway,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(gateway, BrokerGateway):
            raise DomainTypeError("read-only session gateway must satisfy BrokerGateway")
        if not callable(clock):
            raise DomainTypeError("read-only session clock must be callable")
        self._gateway = gateway
        self._clock = clock

    def health(self) -> BrokerHealth:
        return self._gateway.health()

    def query_account(self) -> BrokerAccountFact:
        return self._gateway.query_account()

    def query_positions(self) -> tuple[BrokerPositionFact, ...]:
        return self._gateway.query_positions()

    def query_orders(self) -> tuple[BrokerOrderFact, ...]:
        return self._gateway.query_orders()

    def query_fills(self) -> tuple[BrokerFillFact, ...]:
        return self._gateway.query_fills()

    def query_instrument(self, symbol: Symbol) -> InstrumentFact:
        return self._gateway.query_instrument(symbol)

    def query_quote(self, symbol: Symbol) -> QuoteFact:
        return self._gateway.query_quote(symbol)

    def query_market_status(self) -> MarketSessionStatus:
        return self._gateway.query_market_status()

    def subscribe(self, callback_sink: BrokerEventSink) -> None:
        self._gateway.subscribe(callback_sink)

    def _read(self) -> _BrokerRead:
        return _BrokerRead(
            account=self.query_account(),
            positions=self.query_positions(),
            orders=self.query_orders(),
            fills=self.query_fills(),
        )

    def capture_snapshot(self) -> BrokerSnapshot:
        """Use two equal bounded reads before asserting a complete snapshot."""

        first = self._read()
        second = self._read()
        if _read_signature(first) != _read_signature(second):
            raise BrokerSnapshotUnstable("broker facts changed during bounded SHADOW snapshot capture")
        captured_at = self._clock()
        _aware(captured_at, label="broker snapshot capture time")
        payload = _snapshot_payload(second, captured_at=captured_at)
        raw_payload_sha256 = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        watermark = max(
            (
                *(order.event_sequence for order in second.orders),
                *(fill.event_sequence for fill in second.fills),
                0,
            )
        )
        return BrokerSnapshot(
            snapshot_id="broker-snapshot-" + raw_payload_sha256,
            account=second.account,
            positions=second.positions,
            orders=second.orders,
            fills=second.fills,
            session_date=captured_at.astimezone(_SHANGHAI).date(),
            captured_at=captured_at,
            broker_event_watermark=watermark,
            raw_payload_sha256=raw_payload_sha256,
            complete=True,
        )

    def __repr__(self) -> str:
        return "<ReadOnlyBrokerSession>"


class Runtime:
    """Process-level state and SHADOW composition; no write capability is injected."""

    def __init__(
        self,
        *,
        gateway: BrokerGateway,
        workflow: ShadowWorkflow,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(gateway, BrokerGateway):
            raise DomainTypeError("runtime gateway must satisfy BrokerGateway")
        if not isinstance(workflow, ShadowWorkflow):
            raise DomainTypeError("runtime workflow must satisfy ShadowWorkflow")
        if not callable(clock):
            raise DomainTypeError("runtime clock must be callable")
        self._gateway = gateway
        self._workflow = workflow
        self._clock = clock
        self._read_session = ReadOnlyBrokerSession(gateway=gateway, clock=clock)
        self._status = RuntimeStatus.initial()
        self._mode: Mode | None = None
        self._snapshot: BrokerSnapshot | None = None
        self._reconciliation: ShadowReconciliation | None = None

    @property
    def status(self) -> RuntimeStatus:
        return self._status

    @property
    def mode(self) -> Mode | None:
        return self._mode

    def _now(self) -> datetime:
        value = self._clock()
        _aware(value, label="runtime clock")
        return value

    def start(self, mode: Mode) -> ShadowStartupResult:
        if not isinstance(mode, Mode):
            raise DomainTypeError("runtime mode must be Mode")
        if mode is not Mode.SHADOW:
            raise ModeCompositionError("this read-only composition accepts SHADOW mode only")
        if self._status.state is not RuntimeState.DISARMED:
            raise RuntimeSessionError("runtime is already started")
        self._mode = mode
        self._status = self._status.transition(
            RuntimeState.STARTING,
            reason="SHADOW startup requested",
        )
        try:
            self._gateway.connect()
            self._status = self._status.transition(
                RuntimeState.RECONCILING,
                reason="capturing startup broker facts",
            )
            snapshot = self._read_session.capture_snapshot()
            reconciliation = self._workflow.reconcile_startup(snapshot)
            if not isinstance(reconciliation, ShadowReconciliation):
                raise RuntimeSessionError("shadow workflow returned invalid reconciliation")
            if reconciliation.broker_snapshot_sha256 != snapshot.raw_payload_sha256:
                raise RuntimeSessionError("startup reconciliation references a different broker snapshot")
        except Exception as error:
            self._status = self._status.transition(
                RuntimeState.HALTED,
                reason="SHADOW startup failed closed",
                blockers=("STARTUP_RECONCILIATION_EXCEPTION",),
            )
            raise RuntimeSessionError("SHADOW startup failed") from error
        self._snapshot = snapshot
        self._reconciliation = reconciliation
        if not reconciliation.passed:
            self._status = self._status.transition(
                RuntimeState.HALTED,
                reason="startup reconciliation blocked READY",
                blockers=reconciliation.blockers,
            )
            raise StartupReconciliationFailed(
                "startup reconciliation failed: " + ",".join(reconciliation.blockers)
            )
        self._status = self._status.transition(
            RuntimeState.READY,
            reason="SHADOW startup reconciliation passed",
        )
        return ShadowStartupResult(
            mode=mode,
            runtime_state=self._status.state,
            broker_snapshot=snapshot,
            reconciliation=reconciliation,
        )

    def run_one_session(self) -> ShadowSessionResult:
        if self._mode is not Mode.SHADOW or self._status.state is not RuntimeState.READY:
            raise RuntimeSessionError("SHADOW runtime is not READY")
        snapshot = self._snapshot
        reconciliation = self._reconciliation
        if snapshot is None or reconciliation is None:
            raise RuntimeSessionError("SHADOW startup evidence is unavailable")
        self._status = self._status.transition(
            RuntimeState.EXECUTING,
            reason="generating hypothetical SHADOW execution plan",
        )
        started_at = self._now()
        try:
            decision = self._workflow.decide_after_close(snapshot)
            if not isinstance(decision, DecisionSnapshot):
                raise RuntimeSessionError("shadow workflow returned invalid decision")
            plan = self._workflow.plan_next_session(
                decision,
                self._read_session,
                snapshot,
            )
            if not isinstance(plan, ExecutionPlan):
                raise RuntimeSessionError("shadow workflow returned invalid plan")
            completed_at = self._now()
            draft = ShadowSessionDraft(
                mode=Mode.SHADOW,
                broker_snapshot=snapshot,
                reconciliation=reconciliation,
                decision=decision,
                plan=plan,
                started_at=started_at,
                completed_at=completed_at,
            )
            report = self._workflow.write_shadow_report(draft)
            if not isinstance(report, ShadowReportReceipt):
                raise RuntimeSessionError("shadow workflow returned invalid report receipt")
            if report.payload_sha256 != draft.payload_sha256:
                raise RuntimeSessionError("shadow report digest differs from draft")
        except Exception as error:
            self._status = self._status.transition(
                RuntimeState.HALTED,
                reason="SHADOW planning or report failed closed",
                blockers=("SHADOW_SESSION_FAILED",),
            )
            raise RuntimeSessionError("SHADOW session failed") from error
        self._status = self._status.transition(
            RuntimeState.READY,
            reason="SHADOW hypothetical plan recorded",
        )
        return ShadowSessionResult(
            mode=Mode.SHADOW,
            runtime_state=self._status.state,
            decision_id=decision.decision_id,
            plan_id=plan.plan_id,
            report_id=report.report_id,
            planned_order_count=len(plan.orders),
            planning_blockers=tuple(blocker.reason_code for blocker in plan.blockers),
            plan_created=True,
            report_created=True,
            broker_snapshot_sha256=snapshot.raw_payload_sha256,
            report_payload_sha256=report.payload_sha256,
        )

    def cancel_system_orders(self) -> Never:
        mode = "UNSTARTED" if self._mode is None else self._mode.value
        raise ModeWriteBlocked(f"{mode} composition has no broker cancel capability")

    def stop(self) -> None:
        if self._status.state is RuntimeState.DISARMED:
            return
        self._status = self._status.transition(
            RuntimeState.STOPPING,
            reason="runtime stop requested",
        )
        blocker: tuple[str, ...] = ()
        try:
            self._gateway.disconnect()
        except Exception:
            blocker = ("BROKER_DISCONNECT_FAILED",)
        self._status = self._status.transition(
            RuntimeState.DISARMED,
            reason="runtime stopped",
            blockers=blocker,
        )

    def __repr__(self) -> str:
        return "<Runtime gateway=redacted>"


__all__ = (
    "BrokerSnapshotUnstable",
    "ModeCompositionError",
    "ModeWriteBlocked",
    "ReadOnlyBrokerSession",
    "Runtime",
    "RuntimeErrorBase",
    "RuntimeSessionError",
    "ShadowReconciliation",
    "ShadowReportReceipt",
    "ShadowSessionDraft",
    "ShadowSessionResult",
    "ShadowStartupResult",
    "ShadowWorkflow",
    "StartupReconciliationFailed",
)
