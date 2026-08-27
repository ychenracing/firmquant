"""Concrete fail-closed production composition for SHADOW/CANARY/LIVE."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import signal
import sys
import time as time_module
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol, cast, runtime_checkable
from zoneinfo import ZoneInfo

from firmquant.application.event_pump import DomainEventPump
from firmquant.application.production_daemon import (
    ProductionCycleResult,
    ProductionDaemon,
    ProductionHeartbeat,
)
from firmquant.application.production_events import ProductionEventJournal
from firmquant.application.production_identity import (
    configuration_sha256,
    current_clean_firmquant_commit,
    promotion_config_sha256,
)
from firmquant.application.production_runtime import ProductionRuntime
from firmquant.application.promotion import ShadowPromotionEvidence
from firmquant.application.promotion_store import PromotionStore
from firmquant.broker.gateway import BrokerGateway, BrokerOrderCommand
from firmquant.broker.production_factory import build_production_xtquant_gateway
from firmquant.broker.production_smoke import run_readonly_production_smoke
from firmquant.broker.production_snapshot import ProductionSnapshotCollector
from firmquant.broker.xtquant_safety import XtQuantSafetyManifest
from firmquant.config import Mode, Settings
from firmquant.domain.broker_facts import BrokerPositionFact, BrokerSnapshot, MarketSessionStatus
from firmquant.domain.states import RuntimeState, RuntimeStatus
from firmquant.domain.values import Money, Shares, Symbol
from firmquant.execution.live_controller import (
    ExecutionDeadlines,
    ExecutionWindowPolicy,
    LiveExecutionController,
)
from firmquant.execution.planner import ExecutionBrokerSnapshot, ExecutionPlan, ExecutionPlanner, PlannedOrder
from firmquant.execution.policy import FeeSchedule
from firmquant.market_data.calendar import AuthoritativeTradingCalendar, CalendarCoverageError
from firmquant.market_data.calendar_manifest import load_trading_calendar_manifest
from firmquant.market_data.xtquant_daily import DailyDataUpdateReceipt, XtQuantDailyDataUpdater
from firmquant.market_data.xtquant_history import OfficialXtQuantDailyHistoryProvider
from firmquant.observability.reports import DailyReportRenderer, DatabaseDailyReportBuilder
from firmquant.persistence.account_authority import (
    AccountBindingRepository,
    ReviewedAccountAdjustmentRepository,
)
from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.backup import backup_state
from firmquant.persistence.broker_snapshot_store import BrokerSnapshotStore
from firmquant.persistence.production_recovery import ProductionRecoveryService
from firmquant.persistence.production_repository import MonotonicExecutionLedgerRepository
from firmquant.persistence.recovery import RecoveryContradiction
from firmquant.persistence.repositories import DecisionSnapshotRepository, canonical_json, canonical_sha256
from firmquant.persistence.writer_lease import WriterLease, WriterLeaseGuard, WriterLeaseLost
from firmquant.reconciliation.account_coordinator import (
    AccountReconciliationBlocked,
    AccountReconciliationCoordinator,
)
from firmquant.reconciliation.live_view import build_operational_ledger_view
from firmquant.reconciliation.models import (
    ExpectedPosition,
    ReconciliationFacts,
    ReconciliationKind,
    ReconciliationReceipt,
    StrategyAccountView,
)
from firmquant.reconciliation.service import ReconciliationService
from firmquant.risk.arm import ArmBinding, ArmLease, ArmService
from firmquant.risk.capability import (
    BrokerWriteCapability,
    WriteAuthorizationContext,
    WriteCapabilityFactory,
    WriteOperation,
)
from firmquant.risk.gate import ExecutionRiskContext, ExecutionRiskGate, RiskCommand, RiskLimits
from firmquant.risk.runtime import risk_limits_from_settings
from firmquant.scheduling.clock import ClockGuard, ClockObservation, ClockReceipt
from firmquant.scheduling.clock import ClockGuard, ClockObservation, ClockReceipt
from firmquant.scheduling.sessions import WorkflowReceiptStore
from firmquant.security.secrets import EnvironmentSecretProvider
from firmquant.strategy.account_sync import AccountStateContract
from firmquant.strategy.adapter import DecisionRequest, ProductionEngineContract, StrategyAdapter
from firmquant.strategy.identity import StrategyIdentity
from firmquant.strategy.runtime_account import RuntimeAccountRepository
from firmquant.strategy.snapshots import DecisionSnapshot
from firmquant.strategy.universe import UniversePolicy

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_POST_CLOSE = time(15, 5)
_REFERENCE_SYMBOLS = ("sh000300", "sh000682")
_ACCOUNT_FILE = "uquant-account.json"
_CALENDAR_FILE = "trading-calendar.json"


class ProductionServicesUnavailable(RuntimeError):
    """Required production authority facts or services cannot be proven safe."""


class DailyUpdater(Protocol):
    def update(self, symbols: tuple[str, ...], *, through: date) -> DailyDataUpdateReceipt: ...


@runtime_checkable
class _AccountPayload(Protocol):
    def to_dict(self) -> dict[str, object]: ...


class _UquantExecutionConfig(Protocol):
    max_volume_participation: float


@dataclass(slots=True)
class _StopFlag:
    requested: bool = False

    def __call__(self) -> bool:
        return self.requested

    def request(self, _signum: int, _frame: object) -> None:
        self.requested = True


@dataclass(frozen=True, slots=True)
class _RuntimeIdentity:
    firmquant_commit: str
    uquant_commit: str
    config_sha256: str
    promotion_config_sha256: str
    safety_manifest_sha256: str


@dataclass(frozen=True, slots=True)
class _ExecutionAuthorities:
    plan: ExecutionPlan
    facts: ExecutionBrokerSnapshot
    decision: DecisionSnapshot
    planned: Mapping[str, PlannedOrder]
    reconciliation: ReconciliationReceipt
    reconciliation: ReconciliationReceipt


def _hash_event(prefix: str, payload: object) -> str:
    return prefix + ":" + hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def _fraction(value: Decimal, denominator: Decimal) -> Decimal:
    if denominator <= 0:
        return Decimal(0)
    return min(Decimal(1), max(Decimal(0), value / denominator))


def _count(value: object, *, label: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProductionServicesUnavailable(f"{label}_INVALID")
    return value


def _load_engine(source_checkout: Path, data_directory: Path) -> ProductionEngineContract:
    engine_path = (source_checkout / "uquant" / "engine.py").resolve(strict=True)
    module_name = "firmquant_verified_uquant_engine"
    current = sys.modules.get(module_name)
    if current is not None:
        current_path = Path(str(vars(current).get("__file__", ""))).resolve()
        if current_path != engine_path:
            raise ProductionServicesUnavailable("UQUANT_ENGINE_MODULE_IDENTITY_COLLISION")
        module = current
    else:
        spec = importlib.util.spec_from_file_location(module_name, engine_path)
        if spec is None or spec.loader is None:
            raise ProductionServicesUnavailable("UQUANT_ENGINE_LOAD_FAILED")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
    engine_type = vars(module).get("ProductionEngine")
    config = vars(module).get("DEFAULT_CONFIG")
    if not callable(engine_type) or config is None:
        raise ProductionServicesUnavailable("UQUANT_ENGINE_CONTRACT_UNAVAILABLE")
    return cast(ProductionEngineContract, engine_type(data_directory, config))


def _account_payload(account: object) -> dict[str, object]:
    if not isinstance(account, _AccountPayload):
        raise ProductionServicesUnavailable("UQUANT_ACCOUNT_CONTRACT_UNAVAILABLE")
    payload = account.to_dict()
    if not isinstance(payload, dict):
        raise ProductionServicesUnavailable("UQUANT_ACCOUNT_PAYLOAD_INVALID")
    return payload


def _strategy_view(
    account: object,
    positions: tuple[BrokerPositionFact, ...],
    repository: RuntimeAccountRepository,
) -> StrategyAccountView:
    payload = _account_payload(account)
    raw_cash = payload.get("cash")
    raw_positions = payload.get("positions")
    raw_orders = payload.get("order_ledger")
    if isinstance(raw_cash, bool) or not isinstance(raw_cash, (int, float)):
        raise ProductionServicesUnavailable("UQUANT_ACCOUNT_CASH_INVALID")
    if not isinstance(raw_positions, dict) or not isinstance(raw_orders, list):
        raise ProductionServicesUnavailable("UQUANT_ACCOUNT_STATE_INVALID")
    cash = Money(Decimal(str(raw_cash)))
    broker_positions = {item.symbol.canonical: item for item in positions}
    expected: list[ExpectedPosition] = []
    marked = Decimal(0)
    for raw_symbol, raw_position in sorted(raw_positions.items()):
        if not isinstance(raw_symbol, str) or not isinstance(raw_position, dict):
            raise ProductionServicesUnavailable("UQUANT_ACCOUNT_POSITION_INVALID")
        raw_shares = raw_position.get("shares")
        if isinstance(raw_shares, bool) or not isinstance(raw_shares, int) or raw_shares <= 0:
            raise ProductionServicesUnavailable("UQUANT_ACCOUNT_POSITION_INVALID")
        symbol = Symbol.parse(raw_symbol)
        broker_position = broker_positions.get(symbol.canonical)
        sellable = Shares(0) if broker_position is None else broker_position.sellable_shares
        if broker_position is not None and broker_position.total_shares.value == raw_shares:
            marked += broker_position.market_value.value
        expected.append(ExpectedPosition(symbol, Shares(raw_shares), sellable))
    order_ids: set[str] = set()
    for item in raw_orders:
        if not isinstance(item, dict) or not isinstance(item.get("order_id"), str):
            raise ProductionServicesUnavailable("UQUANT_ACCOUNT_ORDER_LEDGER_INVALID")
        order_ids.add(str(item["order_id"]))
    return StrategyAccountView(
        available_cash=cash,
        total_assets=Money(cash.value + marked),
        positions=tuple(expected),
        known_uquant_order_ids=frozenset(order_ids),
        economic_state_sha256=repository.store.hash_state(account),
    )


def _data_identity_matches(account: object, data_directory: Path) -> bool:
    payload = _account_payload(account)
    digest = payload.get("data_hash")
    as_of = payload.get("data_hash_as_of")
    symbols = payload.get("data_hash_symbols")
    if not isinstance(digest, str) or not digest or not isinstance(as_of, str) or not as_of:
        return False
    if not isinstance(symbols, list) or not symbols or not all(isinstance(item, str) for item in symbols):
        return False
    try:
        module = importlib.import_module("uquant.data")
        factory = vars(module).get("DataStore")
        if not callable(factory):
            return False
        manifest = factory(data_directory).manifest(tuple(symbols), as_of=as_of)
    except Exception:
        return False
    return (
        getattr(manifest, "digest", None) == digest
        and getattr(manifest, "end", None) == as_of
        and tuple(getattr(manifest, "symbols", ())) == tuple(symbols)
    )


def _uquant_participation() -> Decimal:
    module = importlib.import_module("uquant.config")
    config = vars(module).get("DEFAULT_CONFIG")
    if config is None:
        raise ProductionServicesUnavailable("UQUANT_CONFIG_UNAVAILABLE")
    typed = cast(_UquantExecutionConfig, config)
    return Decimal(str(typed.max_volume_participation))


def _fee_schedule(manifest: XtQuantSafetyManifest) -> FeeSchedule:
    return FeeSchedule(
        commission_rate=manifest.commission_rate,
        minimum_commission=manifest.minimum_commission,
        stamp_duty_rate=manifest.stamp_duty_rate,
        transfer_fee_rate=manifest.transfer_fee_rate,
        fee_quantum=Decimal("0.0001"),
    )


def _decision_symbols(decision: DecisionSnapshot) -> tuple[Symbol, ...]:
    raw_orders = decision.uquant_payload.get("orders")
    if not isinstance(raw_orders, list):
        raise ProductionServicesUnavailable("DECISION_ORDER_PAYLOAD_INVALID")
    symbols: set[Symbol] = set()
    for item in raw_orders:
        if not isinstance(item, dict) or not isinstance(item.get("symbol"), str):
            raise ProductionServicesUnavailable("DECISION_ORDER_PAYLOAD_INVALID")
        symbols.add(Symbol.parse(str(item["symbol"])))
    return tuple(sorted(symbols, key=lambda value: value.canonical))


class ProductionServiceHooks:
    """Single production orchestration path owned by the daemon writer thread."""

    def __init__(
        self,
        *,
        config_path: Path,
        settings: Settings,
        writer: WriterLease,
        broker: BrokerGateway,
        calendar: AuthoritativeTradingCalendar,
        account_repository: RuntimeAccountRepository,
        data_updater: DailyUpdater,
        strategy_adapter: StrategyAdapter,
        universe_policy: UniversePolicy,
        event_journal: ProductionEventJournal,
        identity: _RuntimeIdentity,
        safety_manifest: XtQuantSafetyManifest,
        clock: Callable[[], datetime],
        monotonic_clock: Callable[[], float] = time_module.monotonic,
    ) -> None:
        self._config_path = config_path
        self._settings = settings
        self._writer = writer
        self._database = writer.database
        self._broker = broker
        self._calendar = calendar
        self._accounts = account_repository
        self._data_updater = data_updater
        self._strategy = strategy_adapter
        self._universe = universe_policy
        self._journal = event_journal
        self._identity = identity
        self._safety = safety_manifest
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._last_monotonic: float | None = None
        self._disconnect_started_monotonic: float | None = None
        self._last_quote_at: datetime | None = None
        self._snapshots = BrokerSnapshotStore(self._database)
        self._decisions = DecisionSnapshotRepository(self._database)
        self._ledger = MonotonicExecutionLedgerRepository(self._database)
        self._receipts = WorkflowReceiptStore(writer_lease=writer)
        self._reconciler = ReconciliationService(
            database=self._database,
            cash_tolerance=Money(Decimal("0.01")),
            clock=clock,
        )
        self._status = self._receipts.load_runtime(settings.mode)
        self._startup_reconciliation_id: str | None = None
        self._real_order_calls = 0
        self._active_execution_deadlines: ExecutionDeadlines | None = None
        self._active_execution_deadlines: ExecutionDeadlines | None = None

    @property
    def status(self) -> RuntimeStatus:
        return self._status

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ProductionServicesUnavailable("PRODUCTION_CLOCK_INVALID")
        return value

    def _monotonic(self) -> float:
        value = self._monotonic_clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProductionServicesUnavailable("MONOTONIC_CLOCK_INVALID")
        observed = float(value)
        if observed < 0 or (self._last_monotonic is not None and observed < self._last_monotonic):
            raise ProductionServicesUnavailable("MONOTONIC_CLOCK_ROLLBACK")
        self._last_monotonic = observed
        return observed

    def _clock_receipt(self, symbol: Symbol) -> ClockReceipt:
        system_time = self._now()
        quote = self._broker.query_quote(symbol)
        self._last_quote_at = quote.received_at
        try:
            return ClockGuard(
                max_drift=timedelta(seconds=self._settings.execution.max_clock_drift_seconds)
            ).verify(
                ClockObservation(
                    system_time=system_time,
                    reference_time=quote.event_time,
                    local_timezone=self._settings.timezone,
                )
            )
        except Exception as error:
            raise ProductionServicesUnavailable("CLOCK_DRIFT_UNVERIFIED") from error

    def _disconnect_duration(self, *, connected: bool) -> timedelta:
        observed = self._monotonic()
        if connected:
            self._disconnect_started_monotonic = None
            return timedelta(seconds=observed - observed)
        if self._disconnect_started_monotonic is None:
            self._disconnect_started_monotonic = observed
        return timedelta(seconds=observed - self._disconnect_started_monotonic)

    def _existing_order_age(self, now: datetime) -> timedelta | None:
        row = self._database.query_one(
            "SELECT min(created_at) AS created_at FROM execution_intents WHERE state IN "
            "('SUBMITTING','ACKNOWLEDGED','PARTIALLY_FILLED','CANCEL_REQUESTED','UNKNOWN')"
        )
        if row is None or row["created_at"] is None:
            return None
        created_at = datetime.fromisoformat(str(row["created_at"]))
        if created_at.tzinfo is None or created_at.utcoffset() is None or created_at > now:
            raise ProductionServicesUnavailable("EXISTING_ORDER_TIME_INVALID")
        return now - created_at

    def _execution_deadlines(self, now: datetime) -> ExecutionDeadlines | None:
        shanghai = now.astimezone(_SHANGHAI)
        completion_wall = datetime.combine(
            shanghai.date(), time(14, 59, 50), tzinfo=_SHANGHAI
        )
        cancel_wall = completion_wall - timedelta(seconds=30)
        max_window = timedelta(
            seconds=max(
                self._settings.execution.sell_window_seconds,
                self._settings.execution.buy_window_seconds,
            )
        )
        submit_wall = cancel_wall - max_window
        if shanghai >= submit_wall:
            return None
        monotonic_now = self._monotonic()
        return ExecutionDeadlines(
            latest_new_submit=monotonic_now + (submit_wall - shanghai).total_seconds(),
            latest_cancel_initiation=monotonic_now + (cancel_wall - shanghai).total_seconds(),
            absolute_completion=monotonic_now + (completion_wall - shanghai).total_seconds(),
        )

    def _monotonic(self) -> float:
        value = self._monotonic_clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProductionServicesUnavailable("MONOTONIC_CLOCK_INVALID")
        observed = float(value)
        if observed < 0 or (self._last_monotonic is not None and observed < self._last_monotonic):
            raise ProductionServicesUnavailable("MONOTONIC_CLOCK_ROLLBACK")
        self._last_monotonic = observed
        return observed

    def _clock_receipt(self, symbol: Symbol) -> ClockReceipt:
        system_time = self._now()
        quote = self._broker.query_quote(symbol)
        self._last_quote_at = quote.received_at
        try:
            return ClockGuard(
                max_drift=timedelta(seconds=self._settings.execution.max_clock_drift_seconds)
            ).verify(
                ClockObservation(
                    system_time=system_time,
                    reference_time=quote.event_time,
                    local_timezone=self._settings.timezone,
                )
            )
        except Exception as error:
            raise ProductionServicesUnavailable("CLOCK_DRIFT_UNVERIFIED") from error

    def _disconnect_duration(self, *, connected: bool) -> timedelta:
        observed = self._monotonic()
        if connected:
            self._disconnect_started_monotonic = None
            return timedelta(seconds=observed - observed)
        if self._disconnect_started_monotonic is None:
            self._disconnect_started_monotonic = observed
        return timedelta(seconds=observed - self._disconnect_started_monotonic)

    def _existing_order_age(self, now: datetime) -> timedelta | None:
        row = self._database.query_one(
            "SELECT min(created_at) AS created_at FROM execution_intents WHERE state IN "
            "('SUBMITTING','ACKNOWLEDGED','PARTIALLY_FILLED','CANCEL_REQUESTED','UNKNOWN')"
        )
        if row is None or row["created_at"] is None:
            return None
        created_at = datetime.fromisoformat(str(row["created_at"]))
        if created_at.tzinfo is None or created_at.utcoffset() is None or created_at > now:
            raise ProductionServicesUnavailable("EXISTING_ORDER_TIME_INVALID")
        return now - created_at

    def _execution_deadlines(self, now: datetime) -> ExecutionDeadlines | None:
        shanghai = now.astimezone(_SHANGHAI)
        completion_wall = datetime.combine(
            shanghai.date(), time(14, 59, 50), tzinfo=_SHANGHAI
        )
        cancel_wall = completion_wall - timedelta(seconds=30)
        max_window = timedelta(
            seconds=max(
                self._settings.execution.sell_window_seconds,
                self._settings.execution.buy_window_seconds,
            )
        )
        submit_wall = cancel_wall - max_window
        if shanghai >= submit_wall:
            return None
        monotonic_now = self._monotonic()
        return ExecutionDeadlines(
            latest_new_submit=monotonic_now + (submit_wall - shanghai).total_seconds(),
            latest_cancel_initiation=monotonic_now + (cancel_wall - shanghai).total_seconds(),
            absolute_completion=monotonic_now + (completion_wall - shanghai).total_seconds(),
        )

    def _transition(
        self,
        target: RuntimeState,
        *,
        reason: str,
        blockers: tuple[str, ...] = (),
    ) -> None:
        previous = self._status
        current = previous.transition(target, reason=reason, blockers=blockers)
        if current != previous:
            self._receipts.save_runtime(
                mode=self._settings.mode,
                previous=previous,
                current=current,
                created_at=self._now(),
            )
        self._status = current

    def _capture(self) -> BrokerSnapshot:
        snapshot = ProductionSnapshotCollector(
            broker=self._broker,
            clock=self._clock,
            max_attempts=3,
        ).capture()
        self._snapshots.persist(snapshot)
        return snapshot

    def _reconcile(self, kind: ReconciliationKind) -> tuple[ReconciliationReceipt, BrokerSnapshot, object]:
        snapshot = ProductionSnapshotCollector(
            broker=self._broker,
            clock=self._clock,
            max_attempts=3,
        ).capture()
        self._snapshots.persist(snapshot)
        binding = AccountBindingRepository(self._database).load()
        if binding is None:
            raise ProductionServicesUnavailable("ACCOUNT_BINDING_REQUIRED")
        operational = build_operational_ledger_view(
            self._database,
            broker_session=snapshot.session_date,
            expected_account_id_hash=binding.account_id_hash,
            expected_account_type=binding.account_type,
        )
        coordinator = AccountReconciliationCoordinator(
            account_repository=self._accounts,
            reconciler=self._reconciler,
            cash_tolerance=Decimal("0.01"),
            reviewed_adjustments=ReviewedAccountAdjustmentRepository(self._database),
        )

        def final_facts(account: AccountStateContract) -> ReconciliationFacts:
            identity = StrategyIdentity.locked()
            payload = _account_payload(account)
            strategy_view = _strategy_view(account, snapshot.positions, self._accounts)
            broker_positions = {item.symbol: item for item in snapshot.positions}
            strategy_positions = {item.symbol: item for item in strategy_view.positions}
            suspected = frozenset(
                symbol
                for symbol in set(broker_positions) | set(strategy_positions)
                if (
                    (0 if broker_positions.get(symbol) is None else broker_positions[symbol].total_shares.value)
                    != (0 if strategy_positions.get(symbol) is None else strategy_positions[symbol].total_shares.value)
                    or (
                        0
                        if broker_positions.get(symbol) is None
                        else broker_positions[symbol].sellable_shares.value
                    )
                    != (
                        0
                        if strategy_positions.get(symbol) is None
                        else strategy_positions[symbol].sellable_shares.value
                    )
                )
            )
            return ReconciliationFacts(
                broker_snapshot=snapshot,
                strategy_account=strategy_view,
                operational_ledger=operational,
                company_action_suspected_symbols=suspected,
                uquant_code_identity_matches=(
                    payload.get("code_hash") in {"", identity.economic_code_fingerprint}
                ),
                data_identity_matches=_data_identity_matches(
                    account,
                    self._settings.paths.data_directory,
                ),
                config_identity_matches=(
                    configuration_sha256(self._config_path) == self._identity.config_sha256
                ),
            )

        try:
            result = coordinator.reconcile(
                kind=kind,
                snapshot=snapshot,
                operational_ledger=operational,
                binding=binding,
                final_facts=final_facts,
            )
        except AccountReconciliationBlocked as error:
            raise ProductionServicesUnavailable(
                "RECONCILIATION_FAILED:" + ",".join(error.blockers)
            ) from error
        except RecoveryContradiction as error:
            raise ProductionServicesUnavailable("ACCOUNT_COMMIT_CONTRADICTION") from error
        return result.receipt, snapshot, result.account

    def _require_promotion(self, account_hash: str) -> None:
        if self._settings.mode is Mode.SHADOW:
            return
        thresholds = self._settings.promotion
        if not PromotionStore(self._database).qualifies(
            firmquant_commit=self._identity.firmquant_commit,
            uquant_commit=self._identity.uquant_commit,
            config_sha256=self._identity.promotion_config_sha256,
            account_hash=account_hash,
            min_sessions=thresholds.min_shadow_sessions,
            min_orders=thresholds.min_shadow_orders,
            max_tracking_error=thresholds.max_target_tracking_error,
        ):
            raise ProductionServicesUnavailable("SHADOW_PROMOTION_EVIDENCE_REQUIRED")

    def startup(self) -> str:
        self._writer.assert_current()
        if self._status.state in {RuntimeState.HALTED, RuntimeState.STOPPING}:
            raise ProductionServicesUnavailable("EXPLICIT_RESUME_REQUIRED")
        if self._status.state is RuntimeState.DISARMED:
            self._transition(RuntimeState.STARTING, reason="production runtime starting")
        if self._status.state is not RuntimeState.RECONCILING:
            self._transition(RuntimeState.RECONCILING, reason="production startup recovery")
        recovery = ProductionRecoveryService(
            database=self._database,
            account_store=self._accounts.store,
            account_path=self._accounts.path,
            gateway=self._broker,
            clock=self._clock,
        ).recover()
        if recovery.halt_required:
            blockers = tuple(sorted(set(recovery.blockers) | set(recovery.unresolved_order_ids)))
            self._transition(
                RuntimeState.HALTED,
                reason="production recovery blocked startup",
                blockers=blockers or ("RECOVERY_FAILED",),
            )
            raise ProductionServicesUnavailable("PRODUCTION_RECOVERY_FAILED")
        account = self._broker.query_account()
        run_readonly_production_smoke(
            broker=self._broker,
            database=self._database,
            probe_symbol=self._safety.probe_symbol,
            firmquant_commit=self._identity.firmquant_commit,
            uquant_commit=self._identity.uquant_commit,
            config_sha256=self._identity.config_sha256,
            safety_manifest_sha256=self._identity.safety_manifest_sha256,
            clock=self._clock,
        )
        self._require_promotion(account.account_id_hash)
        try:
            receipt, _, _ = self._reconcile(ReconciliationKind.STARTUP)
        except Exception as error:
            self._transition(
                RuntimeState.HALTED,
                reason="production startup reconciliation failed",
                blockers=("STARTUP_RECONCILIATION_FAILED",),
            )
            raise ProductionServicesUnavailable("STARTUP_RECONCILIATION_FAILED") from error
        self._startup_reconciliation_id = receipt.reconciliation_id
        self._transition(RuntimeState.READY, reason="production startup reconciliation passed")
        return receipt.reconciliation_id

    def handle_event(self, event: object) -> None:
        from firmquant.broker.normalization import BrokerEventEnvelope

        if not isinstance(event, BrokerEventEnvelope):
            raise ProductionServicesUnavailable("BROKER_EVENT_TYPE_INVALID")
        self._journal.append(event)
        if self._journal.pending_halt_reason is not None:
            reason = self._journal.pending_halt_reason
            self.halt(reason)
            raise ProductionServicesUnavailable(reason)

    def _audited(self, event_id: str) -> bool:
        return (
            self._database.query_one(
                "SELECT 1 FROM audit_events WHERE audit_event_id = ?",
                (event_id,),
            )
            is not None
        )

    def _audit(self, event_id: str, category: str, payload: Mapping[str, object]) -> None:
        if self._audited(event_id):
            return
        with self._database.transaction():
            AuditLedger(self._database).append(
                audit_event_id=event_id,
                category=category,
                actor="production-services",
                payload=dict(payload),
                created_at=self._now(),
            )

    def _latest_passed_reconciliation(self, kind: ReconciliationKind) -> str:
        row = self._database.query_one(
            "SELECT reconciliation_id FROM reconciliation_runs WHERE kind = ? AND passed = 1 "
            "ORDER BY completed_at DESC, reconciliation_id DESC LIMIT 1",
            (kind.value,),
        )
        if row is None:
            raise ProductionServicesUnavailable("RECONCILIATION_RECEIPT_MISSING")
        return str(row["reconciliation_id"])

    def _post_close_decision(self, session: date) -> int:
        existing = self._decisions.for_session(session)
        if existing:
            if len(existing) != 1:
                raise ProductionServicesUnavailable("MULTIPLE_FROZEN_DECISIONS")
            decision = existing[0]
            account = self._accounts.load()
            actual = self._accounts.store.hash_state(account)
            if actual == decision.account_after_sha256:
                return 0
            if actual != decision.account_before_sha256:
                raise ProductionServicesUnavailable("DECISION_ACCOUNT_RECOVERY_CONTRADICTION")
            recovered = self._strategy.recover_existing_decision(
                DecisionRequest(
                    strategy_session=session,
                    symbols=self._universe.deployment_symbols,
                    account=account,
                    firmquant_commit=decision.firmquant_commit,
                    data_manifest_sha256=decision.data_manifest_sha256,
                    broker_snapshot_sha256=decision.broker_snapshot_sha256,
                    created_at=decision.created_at,
                ),
                decision,
            )
            persisted = self._accounts.persist_prepared(
                account,
                expected_before_sha256=decision.account_before_sha256,
                operation_kind="DECISION_RECOVERY",
                evidence_sha256=decision.payload_sha256,
            )
            if recovered.decision_id != decision.decision_id or persisted != decision.account_after_sha256:
                raise ProductionServicesUnavailable("DECISION_ACCOUNT_RECOVERY_MISMATCH")
            self._audit(
                "production-decision-recovery:" + decision.decision_id,
                "PRODUCTION_DECISION_RECOVERY",
                {
                    "schema": "firmquant.production-decision-recovery.v1",
                    "decision_id": decision.decision_id,
                    "strategy_session": session,
                    "account_after_sha256": decision.account_after_sha256,
                },
            )
            return 0
        symbols = tuple(sorted(set(self._universe.deployment_symbols) | set(_REFERENCE_SYMBOLS)))
        update = self._data_updater.update(symbols, through=session)
        snapshot = self._capture()
        account = self._accounts.load()
        reconciliation_id = self._latest_passed_reconciliation(ReconciliationKind.EOD)
        decision = self._strategy.decide_once(
            DecisionRequest(
                strategy_session=session,
                symbols=self._universe.deployment_symbols,
                account=account,
                firmquant_commit=self._identity.firmquant_commit,
                data_manifest_sha256=update.manifest_sha256,
                broker_snapshot_sha256=snapshot.raw_payload_sha256,
                created_at=self._now(),
            )
        )
        persisted = self._accounts.persist_prepared(
            account,
            expected_before_sha256=decision.account_before_sha256,
            operation_kind="DECISION_COMMIT",
            evidence_sha256=decision.payload_sha256,
        )
        if persisted != decision.account_after_sha256:
            raise ProductionServicesUnavailable("DECISION_ACCOUNT_COMMIT_MISMATCH")
        self._audit(
            "production-decision:" + decision.decision_id,
            "PRODUCTION_DECISION",
            {
                "schema": "firmquant.production-decision.v1",
                "decision_id": decision.decision_id,
                "session": session,
                "data_manifest_sha256": update.manifest_sha256,
                "reconciliation_id": reconciliation_id,
            },
        )
        return 1

    def _execution_facts(self, decision: DecisionSnapshot) -> ExecutionBrokerSnapshot:
        snapshot = self._capture()
        symbols = _decision_symbols(decision)
        return ExecutionBrokerSnapshot(
            broker_snapshot=snapshot,
            instruments=tuple(self._broker.query_instrument(symbol) for symbol in symbols),
            quotes=tuple(self._broker.query_quote(symbol) for symbol in symbols),
            market_status=self._broker.query_market_status(),
        )

    def _load_arm(self, account_hash: str) -> tuple[ArmService, ArmLease, ArmBinding]:
        row = self._database.query_one(
            "SELECT * FROM arm_leases WHERE revoked_at IS NULL ORDER BY issued_at DESC, lease_id DESC LIMIT 1"
        )
        if row is None:
            raise ProductionServicesUnavailable("ACTIVE_ARM_LEASE_REQUIRED")
        try:
            lease = ArmLease(
                lease_id=str(row["lease_id"]),
                mode=Mode(str(row["mode"])),
                host_hash=str(row["host_hash"]),
                account_hash=str(row["account_hash"]),
                firmquant_commit=str(row["firmquant_commit"]),
                uquant_commit=str(row["uquant_commit"]),
                config_sha256=str(row["config_sha256"]),
                identity_payload_sha256=str(row["identity_payload_sha256"]),
                issued_at=datetime.fromisoformat(str(row["issued_at"])),
                expires_at=datetime.fromisoformat(str(row["expires_at"])),
                lease_mac=str(row["lease_mac"]),
            )
            binding = ArmBinding(
                mode=self._settings.mode,
                host_hash=self._writer.host_hash,
                account_hash=account_hash,
                firmquant_commit=self._identity.firmquant_commit,
                uquant_commit=self._identity.uquant_commit,
                config_sha256=self._identity.config_sha256,
            )
            service = ArmService(mac_key=EnvironmentSecretProvider().get_secret("ARM_MAC_KEY"))
            service.verify(lease, binding=binding, now=self._now())
        except Exception as error:
            raise ProductionServicesUnavailable("ACTIVE_ARM_LEASE_INVALID") from error
        required = (
            max(
                self._settings.execution.sell_window_seconds,
                self._settings.execution.buy_window_seconds,
            )
            + self._settings.execution.poll_interval_seconds
            + 1
        )
        if lease.expires_at - self._now() <= timedelta(seconds=required):
            raise ProductionServicesUnavailable("ARM_LEASE_TOO_CLOSE_TO_EXPIRY")
        return service, lease, binding

    def _known_client_ids(self) -> frozenset[str]:
        return frozenset(
            str(row["uquant_order_id"])
            for row in self._database.query_all("SELECT uquant_order_id FROM execution_intents")
        )

    def _external_order_count(self) -> int:
        known = self._known_client_ids()
        return sum(
            1
            for order in self._broker.query_orders()
            if order.client_order_id is None or order.client_order_id not in known
        )

    def _system_cancel_allowed(self, broker_order_id: str) -> bool:
        row = self._database.query_one(
            "SELECT ownership FROM broker_orders WHERE broker_order_id = ?",
            (broker_order_id,),
        )
        return row is not None and row["ownership"] == "SYSTEM"

    def _notionals(self, session: date) -> tuple[Money, Money]:
        rows = self._database.query_all(
            "SELECT requested_shares, filled_shares, limit_price FROM broker_orders "
            "WHERE session_date = ? AND ownership = 'SYSTEM'",
            (session.isoformat(),),
        )
        submitted = Decimal(0)
        filled = Decimal(0)
        for row in rows:
            if row["limit_price"] is None:
                raise ProductionServicesUnavailable("BROKER_ORDER_PRICE_MISSING")
            price = Decimal(str(row["limit_price"]))
            submitted += Decimal(int(row["requested_shares"])) * price
            filled += Decimal(int(row["filled_shares"])) * price
        return Money(submitted), Money(filled)

    def _account_risk_fractions(self, snapshot: BrokerSnapshot) -> tuple[Decimal, Decimal, Decimal]:
        current = snapshot.account.total_assets.value
        previous = self._database.query_one(
            "SELECT c.total_assets FROM broker_snapshots b JOIN cash_snapshots c USING(snapshot_id) "
            "WHERE b.snapshot_id != ? ORDER BY b.captured_at DESC LIMIT 1",
            (snapshot.snapshot_id,),
        )
        previous_assets = current if previous is None else Decimal(str(previous["total_assets"]))
        equity_change = _fraction(abs(current - previous_assets), previous_assets)
        opening = self._database.query_one(
            "SELECT c.total_assets FROM broker_snapshots b JOIN cash_snapshots c USING(snapshot_id) "
            "WHERE b.session_date = ? ORDER BY b.captured_at LIMIT 1",
            (snapshot.session_date.isoformat(),),
        )
        opening_assets = current if opening is None else Decimal(str(opening["total_assets"]))
        intraday_loss = _fraction(max(Decimal(0), opening_assets - current), opening_assets)
        payload = _account_payload(self._accounts.load())
        peak = Decimal(str(payload.get("capital_peak", current)))
        drawdown = _fraction(max(Decimal(0), peak - current), peak)
        return equity_change, intraday_loss, drawdown

    def _risk_context(
        self,
        command: BrokerOrderCommand,
        planned: PlannedOrder,
        authorities: _ExecutionAuthorities,
        snapshot: BrokerSnapshot,
        limits: RiskLimits,
    ) -> ExecutionRiskContext:
        account = snapshot.account
        positions = snapshot.positions
        position = next((item for item in positions if item.symbol == command.symbol), None)
        decision = authorities.decision.uquant_payload
        risk = decision.get("risk")
        envelope = json.loads(authorities.decision.payload_json)
        sentinel = envelope.get("sentinel") if isinstance(envelope, dict) else None
        if not isinstance(risk, dict) or not isinstance(sentinel, dict):
            raise ProductionServicesUnavailable("DECISION_RISK_PAYLOAD_INVALID")
        freeze = sentinel.get("freeze_new_risk")
        if not isinstance(freeze, bool):
            raise ProductionServicesUnavailable("DECISION_SENTINEL_PAYLOAD_INVALID")
        submitted, filled = self._notionals(planned.execution_session)
        equity_change, intraday_loss, drawdown = self._account_risk_fractions(snapshot)
        attempts = _count(
            self._database.scalar(
                "SELECT count(*) FROM broker_order_attempts WHERE started_at >= ?",
                (snapshot.captured_at.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),),
            ),
            label="BROKER_ATTEMPT_COUNT",
        )
        health = self._broker.health()
        observed_now = self._now()
        clock_receipt = self._clock_receipt(command.symbol)
        existing_order_age = self._existing_order_age(observed_now)
        disconnect_duration = self._disconnect_duration(connected=health.connected)
        reconciliation = authorities.reconciliation
        account_state = self._accounts.load()
        actual_gross = sum((item.market_value.value for item in positions), Decimal(0))
        symbol_notional = Decimal(0) if position is None else position.market_value.value
        return ExecutionRiskContext(
            mode=self._settings.mode,
            runtime_state=self._status.state,
            now=observed_now,
            account_type=account.account_type,
            available_cash=account.available_cash,
            total_assets=account.total_assets,
            position=position,
            instrument=self._broker.query_instrument(command.symbol),
            quote=self._broker.query_quote(command.symbol),
            market_status=self._broker.query_market_status(),
            canonical_universe=frozenset(Symbol.parse(item) for item in self._universe.deployment_symbols),
            deployment_allowlist=frozenset(Symbol.parse(item) for item in self._universe.deployment_symbols),
            uquant_target_shares=planned.target_shares,
            uquant_target_weight=planned.target_weight,
            uquant_target_gross=Decimal(str(decision.get("target_gross", 0))),
            uquant_target_gross_cap=Decimal(str(risk.get("target_gross_cap", 0))),
            freeze_new_risk=freeze,
            actual_symbol_notional=Money(symbol_notional),
            actual_gross_notional=Money(actual_gross),
            daily_submitted_notional=submitted,
            daily_filled_notional=filled,
            open_order_count=_count(
                self._database.scalar(
                    "SELECT count(*) FROM execution_intents WHERE state IN "
                    "('SUBMITTING','ACKNOWLEDGED','PARTIALLY_FILLED','CANCEL_REQUESTED')"
                ),
                label="OPEN_ORDER_COUNT",
            ),
            consecutive_rejections=_count(
                self._database.scalar(
                    "SELECT count(*) FROM execution_intents WHERE state = 'REJECTED' "
                    "AND strategy_session = ?",
                    (planned.strategy_session.isoformat(),),
                ),
                label="CONSECUTIVE_REJECTION_COUNT",
            ),
            broker_connected=health.connected,
            disconnect_duration=disconnect_duration,
            existing_order_age=existing_order_age,
            submit_count_window=attempts,
            cancel_count_window=attempts,
            uquant_max_volume_participation=_uquant_participation(),
            equity_change_fraction=equity_change,
            intraday_loss_fraction=intraday_loss,
            capital_drawdown_fraction=drawdown,
            reconciliation_healthy=reconciliation.passed,
            external_active_order_count=self._external_order_count(),
            unexplained_position_change=("UNEXPLAINED_POSITION_CHANGE" in reconciliation.blockers),
            corporate_action_suspected=("CORPORATE_ACTION_SUSPECTED" in reconciliation.blockers),
            clock_drift=timedelta(milliseconds=clock_receipt.drift_milliseconds),
            data_identity_matches=_data_identity_matches(account_state, self._settings.paths.data_directory),
            config_identity_matches=(configuration_sha256(self._config_path) == self._identity.config_sha256),
            unresolved_order_count=_count(
                self._database.scalar(
                    "SELECT count(*) FROM execution_intents WHERE state IN "
                    "('SUBMITTING','CANCEL_REQUESTED','UNKNOWN')"
                ),
                label="UNRESOLVED_ORDER_COUNT",
            ),
            kill_switch_tripped="KILL_SWITCH" in self._status.blockers,
            auction_allowed=False,
            limits=limits,
        )

    def _capability(self, authorities: _ExecutionAuthorities) -> BrokerWriteCapability:
        account_hash = self._broker.query_account().account_id_hash
        arm_service, lease, binding = self._load_arm(account_hash)
        limits = risk_limits_from_settings(self._settings)
        fee_schedule = _fee_schedule(self._safety)
        gate = ExecutionRiskGate()

        def context_provider(operation: WriteOperation, subject: object | None) -> WriteAuthorizationContext:
            now = self._now()
            snapshot = self._capture()
            planned: PlannedOrder | None = None
            gate_decision = None
            quote_time = snapshot.captured_at
            clock_receipt: ClockReceipt | None = None
            symbol_allowed = True
            command_within = True
            cancel_approved = True
            if isinstance(subject, BrokerOrderCommand):
                planned = authorities.planned.get(subject.client_order_id)
                if planned is None:
                    command_within = False
                else:
                    risk_context = self._risk_context(subject, planned, authorities, snapshot, limits)
                    gate_decision = gate.evaluate(
                        RiskCommand(
                            command=subject,
                            uquant_authorized_shares=planned.uquant_authorized_shares,
                            estimated_fees=fee_schedule.calculate(
                                side=subject.side,
                                price=subject.limit_price,
                                shares=subject.requested_shares,
                            ).total,
                        ),
                        risk_context,
                    )
                    quote = self._broker.query_quote(subject.symbol)
                    self._last_quote_at = quote.received_at
                    quote_time = quote.received_at
                    clock_receipt = self._clock_receipt(subject.symbol)
                    symbol_allowed = self._universe.allowed(
                        subject.symbol.canonical, planned.execution_session
                    )
                    command_within = subject.requested_shares.value <= planned.uquant_authorized_shares.value
            elif operation is WriteOperation.CANCEL and isinstance(subject, str):
                cancel_approved = self._system_cancel_allowed(subject)
                broker_order = next(
                    (item for item in snapshot.orders if item.broker_order_id == subject),
                    None,
                )
                if broker_order is None:
                    cancel_approved = False
                else:
                    symbol_allowed = self._universe.allowed(
                        broker_order.symbol.canonical,
                        snapshot.session_date,
                    )
                    quote = self._broker.query_quote(broker_order.symbol)
                    self._last_quote_at = quote.received_at
                    quote_time = quote.received_at
                    clock_receipt = self._clock_receipt(broker_order.symbol)
            known = self._known_client_ids()
            external = any(
                item.client_order_id is None or item.client_order_id not in known for item in snapshot.orders
            )
            attempts = _count(
                self._database.scalar(
                    "SELECT count(*) FROM broker_order_attempts WHERE started_at >= ?",
                    (snapshot.captured_at.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),),
                ),
                label="BROKER_ATTEMPT_COUNT",
            )
            return WriteAuthorizationContext(
                settings=self._settings,
                lease=lease,
                binding=binding,
                now=now,
                runtime_state=self._status.state,
                broker_health=self._broker.health(),
                startup_reconciliation_passed=self._startup_reconciliation_id is not None,
                broker_snapshot_received_at=snapshot.captured_at,
                max_broker_snapshot_age=timedelta(seconds=self._settings.execution.max_quote_age_seconds),
                quote_received_at=quote_time,
                max_quote_age=timedelta(seconds=self._settings.execution.max_quote_age_seconds),
                session_valid=self._calendar.is_trading_session(authorities.plan.execution_session),
                market_status=self._broker.query_market_status(),
                fingerprints_match=(
                    configuration_sha256(self._config_path) == self._identity.config_sha256
                    and StrategyIdentity.locked().uquant_commit == self._identity.uquant_commit
                ),
                kill_switch_tripped="KILL_SWITCH" in self._status.blockers,
                unresolved_order_count=_count(
                    self._database.scalar(
                        "SELECT count(*) FROM execution_intents WHERE state IN ('CANCEL_REQUESTED','UNKNOWN')"
                    ),
                    label="UNRESOLVED_ORDER_COUNT",
                ),
                submitting_unresolved_count=_count(
                    self._database.scalar(
                        "SELECT count(*) FROM execution_intents WHERE state = 'SUBMITTING'"
                    ),
                    label="SUBMITTING_ORDER_COUNT",
                ),
                reconciliation_mismatch=not authorities.reconciliation.passed,
                external_activity_detected=external,
                gate_decision=gate_decision,
                cancel_risk_approved=cancel_approved,
                symbol_in_canonical_universe=symbol_allowed,
                symbol_in_deployment_allowlist=symbol_allowed,
                command_within_uquant_intent=command_within,
                cash_and_positions_safe=(
                    authorities.reconciliation.passed
                    and snapshot.account.available_cash.value >= 0
                    and all(item.sellable_shares.value <= item.total_shares.value for item in snapshot.positions)
                ),
                frequency_within_limits=(
                    attempts < self._settings.execution.max_submit_count_window
                    and attempts < self._settings.execution.max_cancel_count_window
                ),
                clock_receipt=clock_receipt,
            )

        return WriteCapabilityFactory(arm_service=arm_service).create(
            gateway=self._broker,
            context_provider=context_provider,
        )

    def _shadow_execute(self, plan: ExecutionPlan, decision: DecisionSnapshot) -> None:
        account_hash = self._broker.query_account().account_id_hash
        store = PromotionStore(self._database)
        prior = store.latest(
            firmquant_commit=self._identity.firmquant_commit,
            uquant_commit=self._identity.uquant_commit,
            config_sha256=self._identity.promotion_config_sha256,
            account_hash=account_hash,
        )
        store.append(
            ShadowPromotionEvidence(
                firmquant_commit=self._identity.firmquant_commit,
                uquant_commit=self._identity.uquant_commit,
                config_sha256=self._identity.promotion_config_sha256,
                account_hash=account_hash,
                observed_sessions=1 if prior is None else prior.observed_sessions + 1,
                hypothetical_orders=len(plan.orders)
                if prior is None
                else prior.hypothetical_orders + len(plan.orders),
                unresolved_orders=_count(
                    self._database.scalar(
                        "SELECT count(*) FROM execution_intents WHERE state IN "
                        "('SUBMITTING','CANCEL_REQUESTED','UNKNOWN')"
                    ),
                    label="UNRESOLVED_ORDER_COUNT",
                ),
                external_orders=self._external_order_count(),
                duplicate_economic_orders=_count(
                    self._database.scalar(
                        "SELECT count(*) FROM (SELECT decision_id,uquant_order_id FROM execution_intents "
                        "GROUP BY decision_id,uquant_order_id HAVING count(*) > 1)"
                    ),
                    label="DUPLICATE_ECONOMIC_ORDER_COUNT",
                ),
                duplicate_fills=_count(
                    self._database.scalar(
                        "SELECT count(*) FROM (SELECT broker_fill_id FROM fills "
                        "GROUP BY broker_fill_id HAVING count(*) > 1)"
                    ),
                    label="DUPLICATE_FILL_COUNT",
                ),
                max_target_tracking_error=max(
                    Decimal(0) if prior is None else prior.max_target_tracking_error,
                    Decimal(1) if plan.blockers else Decimal(0),
                ),
                created_at=self._now(),
            )
        )
        self._audit(
            "shadow-execution:" + decision.decision_id + ":" + plan.execution_session.isoformat(),
            "SHADOW_EXECUTION",
            {
                "schema": "firmquant.shadow-execution.v1",
                "decision_id": decision.decision_id,
                "execution_session": plan.execution_session,
                "hypothetical_order_count": len(plan.orders),
                "blocker_count": len(plan.blockers),
                "real_order_calls": 0,
            },
        )

    def _execute(self, session: date) -> int:
        try:
            strategy_session = self._calendar.previous_trading_session(session)
        except CalendarCoverageError:
            return 0
        decisions = self._decisions.for_session(strategy_session)
        if not decisions:
            return 0
        if len(decisions) != 1:
            raise ProductionServicesUnavailable("MULTIPLE_FROZEN_DECISIONS")
        decision = decisions[0]
        event_id = "production-execution:" + decision.decision_id + ":" + session.isoformat()
        if self._audited(event_id):
            return 0
        reconciliation, _, _ = self._reconcile(ReconciliationKind.INTRADAY)
        facts = self._execution_facts(decision)
        if (
            facts.broker_snapshot.session_date != session
            or facts.market_status is not MarketSessionStatus.OPEN
        ):
            raise ProductionServicesUnavailable("EXECUTION_MARKET_FACT_INVALID")
        plan = ExecutionPlanner().plan(decision, facts)
        if self._settings.mode is Mode.SHADOW:
            self._shadow_execute(plan, decision)
            self._audit(
                event_id,
                "PRODUCTION_EXECUTION",
                {
                    "schema": "firmquant.production-execution.v1",
                    "decision_id": decision.decision_id,
                    "execution_session": session,
                    "mode": self._settings.mode,
                    "reconciliation_id": reconciliation.reconciliation_id,
                    "real_order_calls": 0,
                },
            )
            return 1
        self._require_promotion(facts.broker_snapshot.account.account_id_hash)
        authorities = _ExecutionAuthorities(
            plan=plan,
            facts=facts,
            decision=decision,
            planned={item.uquant_order_id: item for item in plan.orders},
            reconciliation=reconciliation,
        )
        deadlines = self._active_execution_deadlines
        if deadlines is None:
            raise ProductionServicesUnavailable("EXECUTION_DEADLINE_UNAVAILABLE")
        controller = LiveExecutionController(
            capability=self._capability(authorities),
            ledger=self._ledger,
            fee_schedule=_fee_schedule(self._safety),
            clock=self._clock,
            window_policy=ExecutionWindowPolicy(
                sell_window=timedelta(seconds=self._settings.execution.sell_window_seconds),
                buy_window=timedelta(seconds=self._settings.execution.buy_window_seconds),
                minimum_order_lifetime=timedelta(seconds=self._settings.execution.min_order_lifetime_seconds),
                poll_interval=timedelta(seconds=self._settings.execution.poll_interval_seconds),
            ),
            lease_guard=WriterLeaseGuard(
                self._writer,
                monotonic_clock=self._monotonic_clock,
                renew_interval=timedelta(seconds=10),
            ),
            monotonic_clock=self._monotonic_clock,
            execution_deadlines=deadlines,
            sleep=time_module.sleep,
        )
        result = controller.execute(plan)
        self._real_order_calls += result.submit_calls + result.cancel_calls
        if result.unresolved_unknown or result.negative_cash:
            raise ProductionServicesUnavailable("LIVE_EXECUTION_SAFETY_FAILURE")
        self._audit(
            event_id,
            "PRODUCTION_EXECUTION",
            {
                "schema": "firmquant.production-execution.v1",
                "decision_id": decision.decision_id,
                "execution_session": session,
                "mode": self._settings.mode,
                "reconciliation_id": reconciliation.reconciliation_id,
                "result_sha256": canonical_sha256(
                    {
                        "plan_id": result.plan_id,
                        "outcomes": [
                            {
                                "uquant_order_id": item.uquant_order_id,
                                "execution_id": item.execution_id,
                                "broker_order_id": item.broker_order_id,
                                "reason_code": item.reason_code,
                                "final_state": item.final_state,
                                "filled_shares": item.filled_shares,
                            }
                            for item in result.outcomes
                        ],
                    }
                ),
                "submit_calls": result.submit_calls,
                "cancel_calls": result.cancel_calls,
            },
        )
        return 1

    def _eod(self, session: date) -> int:
        event_id = "production-eod:" + session.isoformat()
        if self._audited(event_id):
            return 0
        receipt, _, _ = self._reconcile(ReconciliationKind.EOD)
        report = DatabaseDailyReportBuilder(self._database, clock=self._clock).build(session)
        rendered = DailyReportRenderer().write(report, self._settings.paths.report_directory)
        backup = backup_state(
            self._database,
            self._settings.paths.backup_directory,
            account_state_path=self._accounts.path,
            created_at=self._now(),
        )
        self._audit(
            event_id,
            "PRODUCTION_EOD",
            {
                "schema": "firmquant.production-eod.v1",
                "session": session,
                "reconciliation_id": receipt.reconciliation_id,
                "report_id": rendered.report_id,
                "backup_id": backup.backup_id,
                "backup_manifest_sha256": backup.manifest_sha256,
            },
        )
        return 1

    def cycle(self, now: datetime) -> ProductionCycleResult:
        if self._status.state is not RuntimeState.READY:
            raise ProductionServicesUnavailable("PRODUCTION_RUNTIME_NOT_READY")
        shanghai = now.astimezone(_SHANGHAI)
        session = shanghai.date()
        try:
            trading = self._calendar.is_trading_session(session)
        except CalendarCoverageError as error:
            self.halt("CALENDAR_COVERAGE_EXPIRED")
            raise ProductionServicesUnavailable("CALENDAR_COVERAGE_EXPIRED") from error
        if not trading:
            return ProductionCycleResult(0, 0, 0)
        market_status = self._broker.query_market_status()
        decisions = executions = eod = 0
        if market_status is MarketSessionStatus.OPEN:
            deadlines = self._execution_deadlines(shanghai)
            if deadlines is None:
                return ProductionCycleResult(0, 0, 0)
            self._active_execution_deadlines = deadlines
            self._transition(RuntimeState.EXECUTING, reason="next-session execution")
            try:
                executions = self._execute(session)
            except WriterLeaseLost:
                raise
            except Exception:
                self.halt("EXECUTION_STEP_FAILED")
                raise
            finally:
                self._active_execution_deadlines = None
            self._transition(RuntimeState.READY, reason="execution step completed")
        elif market_status is MarketSessionStatus.CLOSED and shanghai.time() >= _POST_CLOSE:
            self._transition(RuntimeState.RECONCILING, reason="end-of-day reconciliation")
            try:
                eod = self._eod(session)
            except Exception:
                self.halt("EOD_RECONCILIATION_FAILED")
                raise
            self._transition(RuntimeState.READY, reason="end-of-day reconciliation completed")
            self._transition(RuntimeState.EXECUTING, reason="post-close strategy decision")
            try:
                decisions = self._post_close_decision(session)
            except Exception:
                self.halt("POST_CLOSE_DECISION_FAILED")
                raise
            self._transition(RuntimeState.READY, reason="post-close strategy decision completed")
        return ProductionCycleResult(decisions, executions, eod)

    def heartbeat(self, heartbeat: ProductionHeartbeat) -> None:
        if not isinstance(heartbeat, ProductionHeartbeat):
            raise TypeError("production heartbeat must be typed")
        health = self._broker.health()
        last_broker_event = self._database.scalar("SELECT max(recorded_at) FROM broker_events")
        last_reconciliation = self._database.scalar(
            "SELECT max(completed_at) FROM reconciliation_runs WHERE completed_at IS NOT NULL"
        )
        last_decision = self._database.scalar("SELECT max(created_at) FROM decision_snapshots")
        last_execution = self._database.scalar(
            "SELECT max(created_at) FROM audit_events WHERE category = 'PRODUCTION_EXECUTION'"
        )
        enriched = replace(
            heartbeat,
            runtime_state=self._status.state,
            broker_connected=health.connected,
            broker_read_healthy=health.read_healthy,
            broker_write_healthy=health.write_healthy,
            last_broker_event=None if last_broker_event is None else datetime.fromisoformat(str(last_broker_event)),
            last_quote=self._last_quote_at,
            last_reconciliation=(
                None if last_reconciliation is None else datetime.fromisoformat(str(last_reconciliation))
            ),
            last_decision=None if last_decision is None else datetime.fromisoformat(str(last_decision)),
            last_execution=(
                None if last_execution is None else datetime.fromisoformat(str(last_execution))
            ),
        )
        with self._database.transaction():
            self._database.write(
                """
                INSERT INTO production_heartbeat(
                    singleton_id,mode,runtime_state,observed_at,host_hash,process_id,writer_generation,
                    broker_connected,broker_read_healthy,broker_write_healthy,pending_events,
                    last_broker_event,last_quote,last_reconciliation,last_decision,last_execution,
                    control_request_state,processed_events,decisions,executions,eod
                ) VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(singleton_id) DO UPDATE SET
                    mode=excluded.mode,runtime_state=excluded.runtime_state,observed_at=excluded.observed_at,
                    host_hash=excluded.host_hash,process_id=excluded.process_id,
                    writer_generation=excluded.writer_generation,broker_connected=excluded.broker_connected,
                    broker_read_healthy=excluded.broker_read_healthy,
                    broker_write_healthy=excluded.broker_write_healthy,pending_events=excluded.pending_events,
                    last_broker_event=excluded.last_broker_event,last_quote=excluded.last_quote,
                    last_reconciliation=excluded.last_reconciliation,last_decision=excluded.last_decision,
                    last_execution=excluded.last_execution,control_request_state=excluded.control_request_state,
                    processed_events=excluded.processed_events,decisions=excluded.decisions,
                    executions=excluded.executions,eod=excluded.eod
                """,
                (
                    enriched.mode.value,enriched.runtime_state.value,enriched.observed_at.isoformat(),
                    enriched.host_hash,enriched.process_id,enriched.writer_generation,
                    int(enriched.broker_connected),int(enriched.broker_read_healthy),
                    int(enriched.broker_write_healthy),enriched.pending_events,
                    None if enriched.last_broker_event is None else enriched.last_broker_event.isoformat(),
                    None if enriched.last_quote is None else enriched.last_quote.isoformat(),
                    None if enriched.last_reconciliation is None else enriched.last_reconciliation.isoformat(),
                    None if enriched.last_decision is None else enriched.last_decision.isoformat(),
                    None if enriched.last_execution is None else enriched.last_execution.isoformat(),
                    enriched.control_request_state,enriched.processed_events,enriched.decisions,
                    enriched.executions,enriched.eod,
                ),
            )
        health = self._broker.health()
        last_broker_event = self._database.scalar("SELECT max(recorded_at) FROM broker_events")
        last_reconciliation = self._database.scalar(
            "SELECT max(completed_at) FROM reconciliation_runs WHERE completed_at IS NOT NULL"
        )
        last_decision = self._database.scalar("SELECT max(created_at) FROM decision_snapshots")
        last_execution = self._database.scalar(
            "SELECT max(created_at) FROM audit_events WHERE category = 'PRODUCTION_EXECUTION'"
        )
        enriched = replace(
            heartbeat,
            runtime_state=self._status.state,
            broker_connected=health.connected,
            broker_read_healthy=health.read_healthy,
            broker_write_healthy=health.write_healthy,
            last_broker_event=None if last_broker_event is None else datetime.fromisoformat(str(last_broker_event)),
            last_quote=self._last_quote_at,
            last_reconciliation=(
                None if last_reconciliation is None else datetime.fromisoformat(str(last_reconciliation))
            ),
            last_decision=None if last_decision is None else datetime.fromisoformat(str(last_decision)),
            last_execution=(
                None if last_execution is None else datetime.fromisoformat(str(last_execution))
            ),
        )
        with self._database.transaction():
            self._database.write(
                """
                INSERT INTO production_heartbeat(
                    singleton_id,mode,runtime_state,observed_at,host_hash,process_id,writer_generation,
                    broker_connected,broker_read_healthy,broker_write_healthy,pending_events,
                    last_broker_event,last_quote,last_reconciliation,last_decision,last_execution,
                    control_request_state,processed_events,decisions,executions,eod
                ) VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(singleton_id) DO UPDATE SET
                    mode=excluded.mode,runtime_state=excluded.runtime_state,observed_at=excluded.observed_at,
                    host_hash=excluded.host_hash,process_id=excluded.process_id,
                    writer_generation=excluded.writer_generation,broker_connected=excluded.broker_connected,
                    broker_read_healthy=excluded.broker_read_healthy,
                    broker_write_healthy=excluded.broker_write_healthy,pending_events=excluded.pending_events,
                    last_broker_event=excluded.last_broker_event,last_quote=excluded.last_quote,
                    last_reconciliation=excluded.last_reconciliation,last_decision=excluded.last_decision,
                    last_execution=excluded.last_execution,control_request_state=excluded.control_request_state,
                    processed_events=excluded.processed_events,decisions=excluded.decisions,
                    executions=excluded.executions,eod=excluded.eod
                """,
                (
                    enriched.mode.value,enriched.runtime_state.value,enriched.observed_at.isoformat(),
                    enriched.host_hash,enriched.process_id,enriched.writer_generation,
                    int(enriched.broker_connected),int(enriched.broker_read_healthy),
                    int(enriched.broker_write_healthy),enriched.pending_events,
                    None if enriched.last_broker_event is None else enriched.last_broker_event.isoformat(),
                    None if enriched.last_quote is None else enriched.last_quote.isoformat(),
                    None if enriched.last_reconciliation is None else enriched.last_reconciliation.isoformat(),
                    None if enriched.last_decision is None else enriched.last_decision.isoformat(),
                    None if enriched.last_execution is None else enriched.last_execution.isoformat(),
                    enriched.control_request_state,enriched.processed_events,enriched.decisions,
                    enriched.executions,enriched.eod,
                ),
            )

    def halt(self, reason_code: str) -> None:
        reason = reason_code if isinstance(reason_code, str) and reason_code else "PRODUCTION_HALTED"
        if self._status.state is RuntimeState.HALTED:
            return
        if self._status.state is RuntimeState.DISARMED:
            self._transition(RuntimeState.STARTING, reason="production halt control")
        self._transition(
            RuntimeState.HALTED,
            reason="production runtime halted",
            blockers=(reason,),
        )
        self._audit(
            _hash_event("production-halt", {"reason": reason, "at": self._now()}),
            "RUNTIME",
            {
                "schema": "firmquant.production-halt.v1",
                "mode": self._settings.mode,
                "state": RuntimeState.HALTED,
                "reason": reason,
            },
        )

    def real_order_calls(self) -> int:
        return self._real_order_calls


def _install_stop_handlers(flag: _StopFlag) -> None:
    for name in ("SIGINT", "SIGTERM"):
        observed = getattr(signal, name, None)
        if observed is None:
            continue
        try:
            signal.signal(observed, flag.request)
        except (OSError, RuntimeError, ValueError):
            continue


def build_production_runtime(
    *,
    config_path: Path,
    settings: Settings,
    writer: WriterLease,
    clock: Callable[[], datetime],
) -> ProductionRuntime:
    """Build the sole long-running real-broker runtime from verified local deployment facts."""

    if not isinstance(config_path, Path):
        raise TypeError("production services config path must be Path")
    if not isinstance(settings, Settings):
        raise TypeError("production services settings must be Settings")
    if settings.mode not in {Mode.SHADOW, Mode.CANARY, Mode.LIVE}:
        raise ProductionServicesUnavailable("production services require SHADOW/CANARY/LIVE")
    if not isinstance(writer, WriterLease):
        raise TypeError("production services require active WriterLease")
    if not callable(clock):
        raise TypeError("production services clock must be callable")
    writer.assert_current()
    blockers = settings.xtquant_runtime_blockers()
    if blockers:
        raise ProductionServicesUnavailable(blockers[0])
    source_checkout = settings.paths.uquant_source_checkout
    manifest_path = settings.broker.safety_manifest_path
    if source_checkout is None or manifest_path is None:
        raise ProductionServicesUnavailable("PRODUCTION_IDENTITY_PATHS_MISSING")
    source_checkout = source_checkout.resolve(strict=True)
    safety = XtQuantSafetyManifest.load(manifest_path)
    strategy_identity = StrategyIdentity.locked()
    strategy_identity.verify()
    runtime_identity = _RuntimeIdentity(
        firmquant_commit=current_clean_firmquant_commit(),
        uquant_commit=strategy_identity.uquant_commit,
        config_sha256=configuration_sha256(config_path),
        promotion_config_sha256=promotion_config_sha256(settings),
        safety_manifest_sha256=safety.sha256,
    )
    calendar = load_trading_calendar_manifest(settings.paths.data_directory / _CALENDAR_FILE)
    broker = build_production_xtquant_gateway(
        settings=settings,
        database=writer.database,
        clock=clock,
    )
    try:
        xtdata = importlib.import_module("xtquant.xtdata")
    except (ImportError, ModuleNotFoundError) as error:
        raise ProductionServicesUnavailable("XTQUANT_HISTORY_API_UNAVAILABLE") from error
    data_updater = XtQuantDailyDataUpdater(
        root=settings.paths.data_directory,
        provider=OfficialXtQuantDailyHistoryProvider(
            xtdata=xtdata,
            volume_multipliers=safety.volume_multipliers,
        ),
    )
    observed_now = clock()
    if observed_now.tzinfo is None or observed_now.utcoffset() is None:
        raise ProductionServicesUnavailable("PRODUCTION_CLOCK_INVALID")
    universe = UniversePolicy.from_uquant(
        configured_symbols=None,
        as_of=observed_now.astimezone(_SHANGHAI).date(),
    )
    account_repository = RuntimeAccountRepository(
        database=writer.database,
        path=settings.paths.state_directory / _ACCOUNT_FILE,
        clock=clock,
    )
    strategy = StrategyAdapter(
        engine=_load_engine(source_checkout, settings.paths.data_directory),
        database=writer.database,
        source_checkout=source_checkout,
        universe_policy=universe,
    )
    pump = DomainEventPump(capacity=4096, clock=clock)
    monotonic_clock = time_module.monotonic
    monotonic_clock = time_module.monotonic
    hooks = ProductionServiceHooks(
        config_path=config_path.resolve(),
        settings=settings,
        writer=writer,
        broker=broker,
        calendar=calendar,
        account_repository=account_repository,
        data_updater=data_updater,
        strategy_adapter=strategy,
        universe_policy=universe,
        event_journal=ProductionEventJournal(writer.database),
        identity=runtime_identity,
        safety_manifest=safety,
        clock=clock,
        monotonic_clock=monotonic_clock,
    )
    stop = _StopFlag()
    _install_stop_handlers(stop)
    return ProductionDaemon(
        mode=settings.mode,
        writer=writer,
        broker=broker,
        pump=pump,
        hooks=hooks,
        clock=clock,
        monotonic_clock=monotonic_clock,
        sleep=time_module.sleep,
        stop_requested=stop,
        poll_interval=timedelta(seconds=1),
        renew_interval=timedelta(seconds=10),
    )


__all__ = (
    "ProductionServiceHooks",
    "ProductionServicesUnavailable",
    "build_production_runtime",
)
