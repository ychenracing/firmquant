"""Concrete fail-closed production composition for SHADOW/CANARY/LIVE."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import signal
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast
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
from firmquant.broker.production_smoke import ProductionSmokeStore, run_readonly_production_smoke
from firmquant.broker.production_snapshot import ProductionSnapshotCollector
from firmquant.broker.xtquant_safety import XtQuantSafetyManifest
from firmquant.config import Mode, Settings
from firmquant.domain.broker_facts import (
    AccountType,
    BrokerPositionFact,
    MarketSessionStatus,
    Side,
)
from firmquant.domain.states import RuntimeState, RuntimeStatus
from firmquant.domain.values import Money, Shares, Symbol
from firmquant.execution.live_controller import ExecutionWindowPolicy, LiveExecutionController
from firmquant.execution.planner import ExecutionBrokerSnapshot, ExecutionPlan, ExecutionPlanner, PlannedOrder
from firmquant.execution.policy import FeeSchedule
from firmquant.market_data.calendar import AuthoritativeTradingCalendar, CalendarCoverageError
from firmquant.market_data.calendar_manifest import load_trading_calendar_manifest
from firmquant.market_data.xtquant_daily import DailyDataUpdateReceipt, XtQuantDailyDataUpdater
from firmquant.market_data.xtquant_history import OfficialXtQuantDailyHistoryProvider
from firmquant.observability.reports import DailyReportRenderer, DatabaseDailyReportBuilder
from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.backup import backup_state
from firmquant.persistence.broker_snapshot_store import BrokerSnapshotStore
from firmquant.persistence.database import Database
from firmquant.persistence.production_recovery import ProductionRecoveryService
from firmquant.persistence.production_repository import MonotonicExecutionLedgerRepository
from firmquant.persistence.repositories import DecisionSnapshotRepository, canonical_json, canonical_sha256
from firmquant.persistence.writer_lease import WriterLease
from firmquant.reconciliation.live_view import build_operational_ledger_view
from firmquant.reconciliation.models import (
    ExpectedPosition,
    ReconciliationFacts,
    ReconciliationKind,
    StrategyAccountView,
)
from firmquant.reconciliation.service import ReconciliationService
from firmquant.risk.arm import ArmBinding, ArmLease, ArmService
from firmquant.risk.capability import (
    WriteAuthorizationContext,
    WriteCapabilityFactory,
    WriteOperation,
)
from firmquant.risk.gate import ExecutionRiskContext, ExecutionRiskGate, RiskCommand
from firmquant.risk.runtime import risk_limits_from_settings
from firmquant.scheduling.sessions import WorkflowReceiptStore
from firmquant.security.secrets import EnvironmentSecretProvider
from firmquant.strategy.adapter import DecisionRequest, StrategyAdapter
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


def _hash_event(prefix: str, payload: object) -> str:
    return prefix + ":" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _decimal_fraction(value: Decimal, denominator: Decimal) -> Decimal:
    if denominator <= 0:
        return Decimal(0)
    result = max(Decimal(0), value / denominator)
    return min(result, Decimal(1))


def _load_engine(source_checkout: Path, data_directory: Path) -> object:
    engine_path = (source_checkout / "uquant" / "engine.py").resolve(strict=True)
    module_name = "firmquant_verified_uquant_engine"
    current = sys.modules.get(module_name)
    if current is not None and Path(str(getattr(current, "__file__", ""))).resolve() != engine_path:
        raise ProductionServicesUnavailable("UQUANT_ENGINE_MODULE_IDENTITY_COLLISION")
    if current is None:
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
    else:
        module = cast(ModuleType, current)
    engine_type = getattr(module, "ProductionEngine", None)
    config = getattr(module, "DEFAULT_CONFIG", None)
    if not callable(engine_type) or config is None:
        raise ProductionServicesUnavailable("UQUANT_ENGINE_CONTRACT_UNAVAILABLE")
    return engine_type(data_directory, config)


def _account_payload(account: object) -> dict[str, object]:
    method = getattr(account, "to_dict", None)
    if not callable(method):
        raise ProductionServicesUnavailable("UQUANT_ACCOUNT_CONTRACT_UNAVAILABLE")
    payload = method()
    if not isinstance(payload, dict):
        raise ProductionServicesUnavailable("UQUANT_ACCOUNT_PAYLOAD_INVALID")
    return payload


def _strategy_account_view(
    account: object,
    *,
    snapshot_positions: tuple[BrokerPositionFact, ...],
    account_store: RuntimeAccountRepository,
) -> StrategyAccountView:
    payload = _account_payload(account)
    raw_cash = payload.get("cash")
    if isinstance(raw_cash, bool) or not isinstance(raw_cash, (int, float)):
        raise ProductionServicesUnavailable("UQUANT_ACCOUNT_CASH_INVALID")
    cash = Money(Decimal(str(raw_cash)))
    raw_positions = payload.get("positions")
    if not isinstance(raw_positions, dict):
        raise ProductionServicesUnavailable("UQUANT_ACCOUNT_POSITIONS_INVALID")
    broker_by_symbol = {item.symbol.canonical: item for item in snapshot_positions}
    positions: list[ExpectedPosition] = []
    marked = Decimal(0)
    for raw_symbol, raw_position in sorted(raw_positions.items()):
        if not isinstance(raw_symbol, str) or not isinstance(raw_position, dict):
            raise ProductionServicesUnavailable("UQUANT_ACCOUNT_POSITION_INVALID")
        raw_shares = raw_position.get("shares")
        if isinstance(raw_shares, bool) or not isinstance(raw_shares, int) or raw_shares <= 0:
            raise ProductionServicesUnavailable("UQUANT_ACCOUNT_POSITION_INVALID")
        symbol = Symbol.parse(raw_symbol)
        broker = broker_by_symbol.get(symbol.canonical)
        sellable = Shares(0) if broker is None else broker.sellable_shares
        if broker is not None and broker.total_shares.value == raw_shares:
            marked += broker.market_value.value
        positions.append(
            ExpectedPosition(
                symbol=symbol,
                total_shares=Shares(raw_shares),
                sellable_shares=sellable,
            )
        )
    raw_orders = payload.get("order_ledger")
    if not isinstance(raw_orders, list):
        raise ProductionServicesUnavailable("UQUANT_ACCOUNT_ORDER_LEDGER_INVALID")
    known_ids: set[str] = set()
    for item in raw_orders:
        if not isinstance(item, dict) or not isinstance(item.get("order_id"), str):
            raise ProductionServicesUnavailable("UQUANT_ACCOUNT_ORDER_LEDGER_INVALID")
        known_ids.add(str(item["order_id"]))
    return StrategyAccountView(
        available_cash=cash,
        total_assets=Money(cash.value + marked),
        positions=tuple(positions),
        known_uquant_order_ids=frozenset(known_ids),
        economic_state_sha256=account_store.store.hash_state(account),
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
        factory = getattr(module, "DataStore")
        manifest = factory(data_directory).manifest(tuple(symbols), as_of=as_of)
    except Exception:
        return False
    return (
        getattr(manifest, "digest", None) == digest
        and getattr(manifest, "end", None) == as_of
        and tuple(getattr(manifest, "symbols", ())) == tuple(symbols)
    )


def _config_identity_matches(database: Database, config_sha256: str) -> bool:
    row = database.query_one(
        "SELECT config_sha256 FROM arm_leases WHERE revoked_at IS NULL "
        "ORDER BY issued_at DESC, lease_id DESC LIMIT 1"
    )
    return row is None or str(row["config_sha256"]) == config_sha256


def _fee_schedule(manifest: XtQuantSafetyManifest) -> FeeSchedule:
    return FeeSchedule(
        commission_rate=manifest.commission_rate,
        minimum_commission=manifest.minimum_commission,
        stamp_duty_rate=manifest.stamp_duty_rate,
        transfer_fee_rate=manifest.transfer_fee_rate,
        fee_quantum=Decimal("0.0001"),
    )


def _decision_symbols(decision: DecisionSnapshot) -> tuple[Symbol, ...]:
    payload = decision.uquant_payload
    raw_orders = payload.get("orders")
    if not isinstance(raw_orders, list):
        raise ProductionServicesUnavailable("DECISION_ORDER_PAYLOAD_INVALID")
    result: set[Symbol] = set()
    for item in raw_orders:
        if not isinstance(item, dict) or not isinstance(item.get("symbol"), str):
            raise ProductionServicesUnavailable("DECISION_ORDER_PAYLOAD_INVALID")
        result.add(Symbol.parse(str(item["symbol"])))
    return tuple(sorted(result, key=lambda item: item.canonical))


class ProductionServiceHooks:
    """Single production orchestration path owned by the daemon's writer thread."""

    def __init__(
        self,
        *,
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
    ) -> None:
        self._settings = settings
        self._writer = writer
        self._database = writer.database
        self._broker = broker
        self._calendar = calendar
        self._account_repository = account_repository
        self._data_updater = data_updater
        self._strategy_adapter = strategy_adapter
        self._universe_policy = universe_policy
        self._event_journal = event_journal
        self._identity = identity
        self._safety_manifest = safety_manifest
        self._clock = clock
        self._snapshots = BrokerSnapshotStore(self._database)
        self._decisions = DecisionSnapshotRepository(self._database)
        self._ledger = MonotonicExecutionLedgerRepository(self._database)
        self._receipts = WorkflowReceiptStore(writer_lease=writer)
        self._reconciliation = ReconciliationService(
            database=self._database,
            cash_tolerance=Money(Decimal("0.01")),
            clock=clock,
        )
        self._status = self._receipts.load_runtime(settings.mode)
        self._startup_reconciliation_id: str | None = None
        self._real_order_calls = 0
        self._latest_heartbeat: ProductionHeartbeat | None = None

    @property
    def status(self) -> RuntimeStatus:
        return self._status

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ProductionServicesUnavailable("PRODUCTION_CLOCK_INVALID")
        return value

    def _transition(
        self,
        target: RuntimeState,
        *,
        reason: str,
        blockers: tuple[str, ...] = (),
    ) -> None:
        now = self._now()
        previous = self._status
        current = previous.transition(target, reason=reason, blockers=blockers)
        if current != previous:
            self._receipts.save_runtime(
                mode=self._settings.mode,
                previous=previous,
                current=current,
                created_at=now,
            )
        self._status = current

    def _capture(self):
        snapshot = ProductionSnapshotCollector(
            broker=self._broker,
            clock=self._clock,
            max_attempts=3,
        ).capture()
        self._snapshots.persist(snapshot)
        return snapshot

    def _reconcile(self, kind: ReconciliationKind):
        snapshot = self._capture()
        expected_id, expected_type = self._snapshots.previous_account_identity(snapshot)
        account, _sync = self._account_repository.sync_broker_snapshot(snapshot)
        identity = StrategyIdentity.locked()
        payload = _account_payload(account)
        facts = ReconciliationFacts(
            broker_snapshot=snapshot,
            strategy_account=_strategy_account_view(
                account,
                snapshot_positions=snapshot.positions,
                account_store=self._account_repository,
            ),
            operational_ledger=build_operational_ledger_view(
                self._database,
                broker_session=snapshot.session_date,
                expected_account_id_hash=expected_id,
                expected_account_type=expected_type,
            ),
            company_action_suspected_symbols=frozenset(),
            uquant_code_identity_matches=(
                payload.get("code_hash") in {"", identity.economic_code_fingerprint}
            ),
            data_identity_matches=_data_identity_matches(account, self._settings.paths.data_directory),
            config_identity_matches=_config_identity_matches(
                self._database,
                self._identity.config_sha256,
            ),
        )
        receipt = self._reconciliation.run(kind, facts)
        if not receipt.passed:
            raise ProductionServicesUnavailable(
                "RECONCILIATION_FAILED:" + ",".join(receipt.blockers)
            )
        return receipt, snapshot, account

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
            account_store=self._account_repository.store,
            account_path=self._account_repository.path,
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
            probe_symbol=self._safety_manifest.probe_symbol,
            firmquant_commit=self._identity.firmquant_commit,
            uquant_commit=self._identity.uquant_commit,
            config_sha256=self._identity.config_sha256,
            safety_manifest_sha256=self._identity.safety_manifest_sha256,
            clock=self._clock,
        )
        self._require_promotion(account.account_id_hash)
        try:
            receipt, _snapshot, _account = self._reconcile(ReconciliationKind.STARTUP)
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
        self._event_journal.append(event)
        halt_reason = self._event_journal.pending_halt_reason
        if halt_reason is not None:
            self.halt(halt_reason)
            raise ProductionServicesUnavailable(halt_reason)

    def _audit_completed(self, event_id: str) -> bool:
        return self._database.query_one(
            "SELECT 1 FROM audit_events WHERE audit_event_id = ?",
            (event_id,),
        ) is not None

    def _audit(
        self,
        *,
        event_id: str,
        category: str,
        payload: Mapping[str, object],
        created_at: datetime,
    ) -> None:
        with self._database.transaction():
            if self._database.query_one(
                "SELECT 1 FROM audit_events WHERE audit_event_id = ?",
                (event_id,),
            ) is None:
                AuditLedger(self._database).append(
                    audit_event_id=event_id,
                    category=category,
                    actor="production-services",
                    payload=dict(payload),
                    created_at=created_at,
                )

    def _post_close_decision(self, session: date) -> int:
        if self._decisions.for_session(session):
            return 0
        symbols = tuple(sorted(set(self._universe_policy.deployment_symbols) | set(_REFERENCE_SYMBOLS)))
        update = self._data_updater.update(symbols, through=session)
        reconciliation, snapshot, account = self._reconcile(ReconciliationKind.EOD)
        request = DecisionRequest(
            strategy_session=session,
            symbols=self._universe_policy.deployment_symbols,
            account=account,
            firmquant_commit=self._identity.firmquant_commit,
            data_manifest_sha256=update.manifest_sha256,
            broker_snapshot_sha256=snapshot.raw_payload_sha256,
            created_at=self._now(),
        )
        decision = self._strategy_adapter.decide_once(request)
        persisted = self._account_repository.persist_prepared(
            account,
            expected_before_sha256=decision.account_before_sha256,
            operation_kind="DECISION_COMMIT",
            evidence_sha256=decision.payload_sha256,
        )
        if persisted != decision.account_after_sha256:
            raise ProductionServicesUnavailable("DECISION_ACCOUNT_COMMIT_MISMATCH")
        event_id = "production-decision:" + decision.decision_id
        self._audit(
            event_id=event_id,
            category="PRODUCTION_DECISION",
            payload={
                "schema": "firmquant.production-decision.v1",
                "decision_id": decision.decision_id,
                "session": session,
                "data_manifest_sha256": update.manifest_sha256,
                "reconciliation_id": reconciliation.reconciliation_id,
            },
            created_at=self._now(),
        )
        return 1

    def _execution_facts(self, decision: DecisionSnapshot) -> ExecutionBrokerSnapshot:
        snapshot = self._capture()
        symbols = _decision_symbols(decision)
        instruments = tuple(self._broker.query_instrument(symbol) for symbol in symbols)
        quotes = tuple(self._broker.query_quote(symbol) for symbol in symbols)
        return ExecutionBrokerSnapshot(
            broker_snapshot=snapshot,
            instruments=instruments,
            quotes=quotes,
            market_status=self._broker.query_market_status(),
        )

    def _load_arm(self, account_hash: str) -> tuple[ArmService, ArmLease, ArmBinding]:
        row = self._database.query_one(
            "SELECT * FROM arm_leases WHERE revoked_at IS NULL "
            "ORDER BY issued_at DESC, lease_id DESC LIMIT 1"
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
            service = ArmService(
                mac_key=EnvironmentSecretProvider().get_secret("ARM_MAC_KEY")
            )
            service.verify(lease, binding=binding, now=self._now())
        except Exception as error:
            raise ProductionServicesUnavailable("ACTIVE_ARM_LEASE_INVALID") from error
        required_window = max(
            self._settings.execution.sell_window_seconds,
            self._settings.execution.buy_window_seconds,
        ) + self._settings.execution.poll_interval_seconds + 1
        if lease.expires_at - self._now() <= timedelta(seconds=required_window):
            raise ProductionServicesUnavailable("ARM_LEASE_TOO_CLOSE_TO_EXPIRY")
        return service, lease, binding

    def _latest_reconciliation_healthy(self) -> bool:
        row = self._database.query_one(
            "SELECT passed FROM reconciliation_runs ORDER BY completed_at DESC, reconciliation_id DESC LIMIT 1"
        )
        return row is not None and row["passed"] == 1

    def _risk_context(
        self,
        *,
        command: BrokerOrderCommand,
        planned: PlannedOrder,
        authorities: _ExecutionAuthorities,
    ) -> ExecutionRiskContext:
        account = self._broker.query_account()
        positions = self._broker.query_positions()
        position = next((item for item in positions if item.symbol == command.symbol), None)
        instrument = self._broker.query_instrument(command.symbol)
        quote = self._broker.query_quote(command.symbol)
        health = self._broker.health()
        decision_payload = authorities.decision.uquant_payload
        risk = decision_payload.get("risk")
        sentinel = json.loads(authorities.decision.payload_json).get("sentinel")
        if not isinstance(risk, dict) or not isinstance(sentinel, dict):
            raise ProductionServicesUnavailable("DECISION_RISK_PAYLOAD_INVALID")
        target_gross = Decimal(str(decision_payload.get("target_gross", 0)))
        target_gross_cap = Decimal(str(risk.get("target_gross_cap", 0)))
        freeze = sentinel.get("freeze_new_risk")
        if not isinstance(freeze, bool):
            raise ProductionServicesUnavailable("DECISION_SENTINEL_PAYLOAD_INVALID")
        actual_gross = sum((item.market_value.value for item in positions), Decimal(0))
        symbol_notional = Decimal(0) if position is None else position.market_value.value
        rows = self._database.query_all(
            "SELECT requested_shares, filled_shares FROM execution_intents WHERE strategy_session = ?",
            (planned.strategy_session.isoformat(),),
        )
        submitted = sum(
            (Decimal(int(row["requested_shares"])) * command.limit_price.value for row in rows),
            Decimal(0),
        )
        filled = sum(
            (Decimal(int(row["filled_shares"])) * command.limit_price.value for row in rows),
            Decimal(0),
        )
        open_orders = int(
            self._database.scalar(
                "SELECT count(*) FROM execution_intents WHERE state IN ('SUBMITTING','ACKNOWLEDGED','PARTIALLY_FILLED','CANCEL_REQUESTED')"
            )
            or 0
        )
        unresolved = int(
            self._database.scalar(
                "SELECT count(*) FROM execution_intents WHERE state IN ('SUBMITTING','CANCEL_REQUESTED','UNKNOWN')"
            )
            or 0
        )
        attempts = int(
            self._database.scalar(
                "SELECT count(*) FROM broker_order_attempts WHERE started_at >= ?",
                (authorities.facts.broker_snapshot.captured_at.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),),
            )
            or 0
        )
        account_payload = _account_payload(self._account_repository.load())
        capital_peak = Decimal(str(account_payload.get("capital_peak", account.total_assets.value)))
        capital_drawdown = _decimal_fraction(
            max(Decimal(0), capital_peak - account.total_assets.value),
            capital_peak,
        )
        earliest = self._database.query_one(
            "SELECT c.total_assets FROM broker_snapshots b JOIN cash_snapshots c USING(snapshot_id) "
            "WHERE b.session_date = ? ORDER BY b.captured_at LIMIT 1",
            (planned.execution_session.isoformat(),),
        )
        opening_assets = account.total_assets.value if earliest is None else Decimal(str(earliest["total_assets"]))
        intraday_loss = _decimal_fraction(
            max(Decimal(0), opening_assets - account.total_assets.value),
            opening_assets,
        )
        previous = self._database.query_one(
            "SELECT c.total_assets FROM broker_snapshots b JOIN cash_snapshots c USING(snapshot_id) "
            "WHERE b.snapshot_id != ? ORDER BY b.captured_at DESC LIMIT 1",
            (authorities.facts.broker_snapshot.snapshot_id,),
        )
        previous_assets = account.total_assets.value if previous is None else Decimal(str(previous["total_assets"]))
        equity_change = _decimal_fraction(
            abs(account.total_assets.value - previous_assets),
            previous_assets,
        )
        external = 0
        known_client_ids = {
            str(row["uquant_order_id"])
            for row in self._database.query_all("SELECT uquant_order_id FROM execution_intents")
        }
        for broker_order in self._broker.query_orders():
            if broker_order.client_order_id is None or broker_order.client_order_id not in known_client_ids:
                external += 1
        config_matches = configuration_sha256(self._config_path) == self._identity.config_sha256
        return ExecutionRiskContext(
            mode=self._settings.mode,
            runtime_state=self._status.state,
            now=self._now(),
            account_type=account.account_type,
            available_cash=account.available_cash,
            total_assets=account.total_assets,
            position=position,
            instrument=instrument,
            quote=quote,
            market_status=self._broker.query_market_status(),
            canonical_universe=frozenset(Symbol.parse(item) for item in self._universe_policy.canonical_symbols),
            deployment_allowlist=frozenset(Symbol.parse(item) for item in self._universe_policy.deployment_symbols),
            uquant_target_shares=planned.target_shares,
            uquant_target_weight=planned.target_weight,
            uquant_target_gross=target_gross,
            uquant_target_gross_cap=target_gross_cap,
            freeze_new_risk=freeze,
            actual_symbol_notional=Money(symbol_notional),
            actual_gross_notional=Money(actual_gross),
            daily_submitted_notional=Money(submitted),
            daily_filled_notional=Money(filled),
            open_order_count=open_orders,
            consecutive_rejections=0,
            broker_connected=health.connected,
            disconnect_duration=timedelta(0) if health.connected else timedelta.max,
            existing_order_age=None,
            replacement_count=0,
            submit_count_window=attempts,
            cancel_count_window=attempts,
            uquant_max_volume_participation=Decimal(str(importlib.import_module("uquant.config").DEFAULT_CONFIG.max_volume_participation)),
            equity_change_fraction=equity_change,
            intraday_loss_fraction=intraday_loss,
            capital_drawdown_fraction=capital_drawdown,
            reconciliation_healthy=self._latest_reconciliation_healthy(),
            external_active_order_count=external,
            unexplained_position_change=False,
            corporate_action_suspected=False,
            clock_drift=timedelta(0),
            data_identity_matches=_data_identity_matches(
                self._account_repository.load(),
                self._settings.paths.data_directory,
            ),
            config_identity_matches=config_matches,
            unresolved_order_count=unresolved,
            kill_switch_tripped="KILL_SWITCH" in self._status.blockers,
            auction_allowed=False,
            limits=risk_limits_from_settings(self._settings),
        )

    def _capability(self, authorities: _ExecutionAuthorities):
        account_hash = self._broker.query_account().account_id_hash
        arm_service, lease, binding = self._load_arm(account_hash)
        fee_schedule = _fee_schedule(self._safety_manifest)
        gate = ExecutionRiskGate()

        def context_provider(
            operation: WriteOperation,
            subject: object | None,
        ) -> WriteAuthorizationContext:
            now = self._now()
            health = self._broker.health()
            planned: PlannedOrder | None = None
            gate_decision = None
            quote_time = authorities.facts.broker_snapshot.captured_at
            symbol_allowed = True
            command_within = True
            if isinstance(subject, BrokerOrderCommand):
                planned = authorities.planned.get(subject.client_order_id)
                if planned is None:
                    command_within = False
                else:
                    risk_context = self._risk_context(
                        command=subject,
                        planned=planned,
                        authorities=authorities,
                    )
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
                    quote_time = self._broker.query_quote(subject.symbol).received_at
                    symbol_allowed = self._universe_policy.allowed(
                        subject.symbol.canonical,
                        planned.execution_session,
                    )
                    command_within = (
                        subject.requested_shares.value <= planned.uquant_authorized_shares.value
                    )
            unresolved = int(
                self._database.scalar(
                    "SELECT count(*) FROM execution_intents WHERE state IN ('CANCEL_REQUESTED','UNKNOWN')"
                )
                or 0
            )
            submitting = int(
                self._database.scalar(
                    "SELECT count(*) FROM execution_intents WHERE state = 'SUBMITTING'"
                )
                or 0
            )
            external = any(
                order.client_order_id is None
                for order in self._broker.query_orders()
            )
            return WriteAuthorizationContext(
                settings=self._settings,
                lease=lease,
                binding=binding,
                now=now,
                runtime_state=self._status.state,
                broker_health=health,
                startup_reconciliation_passed=self._startup_reconciliation_id is not None,
                broker_snapshot_received_at=authorities.facts.broker_snapshot.captured_at,
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
                unresolved_order_count=unresolved,
                submitting_unresolved_count=submitting,
                reconciliation_mismatch=not self._latest_reconciliation_healthy(),
                external_activity_detected=external,
                gate_decision=gate_decision,
                cancel_risk_approved=True,
                symbol_in_canonical_universe=symbol_allowed,
                symbol_in_deployment_allowlist=symbol_allowed,
                command_within_uquant_intent=command_within,
                cash_and_positions_safe=True,
                frequency_within_limits=True,
            )

        return WriteCapabilityFactory(arm_service=arm_service).create(
            gateway=self._broker,
            context_provider=context_provider,
        )

    @property
    def _config_path(self) -> Path:
        return self._settings.paths.state_directory.parent / "firmquant.toml"

    def _shadow_execute(self, plan: ExecutionPlan, decision: DecisionSnapshot) -> None:
        account_hash = self._broker.query_account().account_id_hash
        store = PromotionStore(self._database)
        prior = store.latest(
            firmquant_commit=self._identity.firmquant_commit,
            uquant_commit=self._identity.uquant_commit,
            config_sha256=self._identity.promotion_config_sha256,
            account_hash=account_hash,
        )
        unresolved = int(
            self._database.scalar(
                "SELECT count(*) FROM execution_intents WHERE state IN ('SUBMITTING','CANCEL_REQUESTED','UNKNOWN')"
            )
            or 0
        )
        external = sum(1 for order in self._broker.query_orders() if order.client_order_id is None)
        duplicate_orders = int(
            self._database.scalar(
                "SELECT count(*) FROM (SELECT decision_id,uquant_order_id FROM execution_intents "
                "GROUP BY decision_id,uquant_order_id HAVING count(*) > 1)"
            )
            or 0
        )
        duplicate_fills = int(
            self._database.scalar(
                "SELECT count(*) FROM (SELECT broker_fill_id FROM fills GROUP BY broker_fill_id HAVING count(*) > 1)"
            )
            or 0
        )
        prior_sessions = 0 if prior is None else prior.observed_sessions
        prior_orders = 0 if prior is None else prior.hypothetical_orders
        prior_error = Decimal(0) if prior is None else prior.max_target_tracking_error
        current_error = Decimal(1) if plan.blockers else Decimal(0)
        store.append(
            ShadowPromotionEvidence(
                firmquant_commit=self._identity.firmquant_commit,
                uquant_commit=self._identity.uquant_commit,
                config_sha256=self._identity.promotion_config_sha256,
                account_hash=account_hash,
                observed_sessions=prior_sessions + 1,
                hypothetical_orders=prior_orders + len(plan.orders),
                unresolved_orders=unresolved,
                external_orders=external,
                duplicate_economic_orders=duplicate_orders,
                duplicate_fills=duplicate_fills,
                max_target_tracking_error=max(prior_error, current_error),
                created_at=self._now(),
            )
        )
        self._audit(
            event_id="shadow-execution:" + decision.decision_id + ":" + plan.execution_session.isoformat(),
            category="SHADOW_EXECUTION",
            payload={
                "schema": "firmquant.shadow-execution.v1",
                "decision_id": decision.decision_id,
                "execution_session": plan.execution_session,
                "hypothetical_order_count": len(plan.orders),
                "blocker_count": len(plan.blockers),
                "real_order_calls": 0,
            },
            created_at=self._now(),
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
        if self._audit_completed(event_id):
            return 0
        reconciliation, _snapshot, _account = self._reconcile(ReconciliationKind.INTRADAY)
        facts = self._execution_facts(decision)
        if facts.broker_snapshot.session_date != session or facts.market_status is not MarketSessionStatus.OPEN:
            raise ProductionServicesUnavailable("EXECUTION_MARKET_FACT_INVALID")
        plan = ExecutionPlanner().plan(decision, facts)
        if self._settings.mode is Mode.SHADOW:
            self._shadow_execute(plan, decision)
            self._audit(
                event_id=event_id,
                category="PRODUCTION_EXECUTION",
                payload={
                    "schema": "firmquant.production-execution.v1",
                    "decision_id": decision.decision_id,
                    "execution_session": session,
                    "mode": self._settings.mode,
                    "reconciliation_id": reconciliation.reconciliation_id,
                    "real_order_calls": 0,
                },
                created_at=self._now(),
            )
            return 1
        self._require_promotion(facts.broker_snapshot.account.account_id_hash)
        authorities = _ExecutionAuthorities(
            plan=plan,
            facts=facts,
            decision=decision,
            planned={item.uquant_order_id: item for item in plan.orders},
        )
        capability = self._capability(authorities)
        controller = LiveExecutionController(
            capability=capability,
            ledger=self._ledger,
            fee_schedule=_fee_schedule(self._safety_manifest),
            clock=self._clock,
            window_policy=ExecutionWindowPolicy(
                sell_window=timedelta(seconds=self._settings.execution.sell_window_seconds),
                buy_window=timedelta(seconds=self._settings.execution.buy_window_seconds),
                minimum_order_lifetime=timedelta(
                    seconds=self._settings.execution.min_order_lifetime_seconds
                ),
                poll_interval=timedelta(seconds=self._settings.execution.poll_interval_seconds),
            ),
        )
        result = controller.execute(plan)
        self._real_order_calls += result.submit_calls + result.cancel_calls
        if result.unresolved_unknown or result.negative_cash:
            raise ProductionServicesUnavailable("LIVE_EXECUTION_SAFETY_FAILURE")
        output = canonical_sha256(
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
                "submit_calls": result.submit_calls,
                "cancel_calls": result.cancel_calls,
            }
        )
        self._audit(
            event_id=event_id,
            category="PRODUCTION_EXECUTION",
            payload={
                "schema": "firmquant.production-execution.v1",
                "decision_id": decision.decision_id,
                "execution_session": session,
                "mode": self._settings.mode,
                "reconciliation_id": reconciliation.reconciliation_id,
                "result_sha256": output,
                "submit_calls": result.submit_calls,
                "cancel_calls": result.cancel_calls,
            },
            created_at=self._now(),
        )
        return 1

    def _eod(self, session: date) -> int:
        event_id = "production-eod:" + session.isoformat()
        if self._audit_completed(event_id):
            return 0
        receipt, _snapshot, _account = self._reconcile(ReconciliationKind.EOD)
        report = DatabaseDailyReportBuilder(self._database, clock=self._clock).build(session)
        rendered = DailyReportRenderer().write(report, self._settings.paths.report_directory)
        backup = backup_state(
            self._database,
            self._settings.paths.backup_directory,
            account_state_path=self._account_repository.path,
            created_at=self._now(),
        )
        self._audit(
            event_id=event_id,
            category="PRODUCTION_EOD",
            payload={
                "schema": "firmquant.production-eod.v1",
                "session": session,
                "reconciliation_id": receipt.reconciliation_id,
                "report_id": rendered.report_id,
                "backup_id": backup.backup_id,
                "backup_manifest_sha256": backup.manifest_sha256,
            },
            created_at=self._now(),
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
            return ProductionCycleResult(decisions=0, executions=0, eod=0)
        status = self._broker.query_market_status()
        decisions = executions = eod = 0
        if status is MarketSessionStatus.OPEN:
            self._transition(RuntimeState.EXECUTING, reason="next-session execution")
            try:
                executions = self._execute(session)
            except Exception:
                self.halt("EXECUTION_STEP_FAILED")
                raise
            self._transition(RuntimeState.READY, reason="execution step completed")
        elif status is MarketSessionStatus.CLOSED and shanghai.time() >= _POST_CLOSE:
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
        return ProductionCycleResult(decisions=decisions, executions=executions, eod=eod)

    def heartbeat(self, heartbeat: ProductionHeartbeat) -> None:
        if not isinstance(heartbeat, ProductionHeartbeat):
            raise TypeError("production heartbeat must be typed")
        self._latest_heartbeat = heartbeat

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
            event_id=_hash_event("production-halt", {"reason": reason, "at": self._now()}),
            category="RUNTIME",
            payload={
                "schema": "firmquant.production-halt.v1",
                "mode": self._settings.mode,
                "state": RuntimeState.HALTED,
                "reason": reason,
            },
            created_at=self._now(),
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
    safety_manifest = XtQuantSafetyManifest.load(manifest_path)
    identity = StrategyIdentity.locked()
    identity.verify()
    firmquant_commit = current_clean_firmquant_commit()
    config_digest = configuration_sha256(config_path)
    runtime_identity = _RuntimeIdentity(
        firmquant_commit=firmquant_commit,
        uquant_commit=identity.uquant_commit,
        config_sha256=config_digest,
        promotion_config_sha256=promotion_config_sha256(settings),
        safety_manifest_sha256=safety_manifest.sha256,
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
            volume_multipliers=safety_manifest.volume_multipliers,
        ),
    )
    universe_policy = UniversePolicy.from_uquant(configured_symbols=None)
    engine = _load_engine(source_checkout, settings.paths.data_directory)
    strategy_adapter = StrategyAdapter(
        engine=engine,
        database=writer.database,
        source_checkout=source_checkout,
        universe_policy=universe_policy,
    )
    account_repository = RuntimeAccountRepository(
        database=writer.database,
        path=settings.paths.state_directory / _ACCOUNT_FILE,
        clock=clock,
    )
    event_pump = DomainEventPump(capacity=4096, clock=clock)
    hooks = ProductionServiceHooks(
        settings=settings,
        writer=writer,
        broker=broker,
        calendar=calendar,
        account_repository=account_repository,
        data_updater=data_updater,
        strategy_adapter=strategy_adapter,
        universe_policy=universe_policy,
        event_journal=ProductionEventJournal(writer.database),
        identity=runtime_identity,
        safety_manifest=safety_manifest,
        clock=clock,
    )
    # Preserve the exact config path used to form the runtime identity.
    object.__setattr__(hooks, "_config_path_value", config_path.resolve())
    hooks._config_path = config_path.resolve()  # type: ignore[misc,assignment]
    stop = _StopFlag()
    _install_stop_handlers(stop)
    return ProductionDaemon(
        mode=settings.mode,
        writer=writer,
        broker=broker,
        pump=event_pump,
        hooks=hooks,
        clock=clock,
        sleep=lambda seconds: __import__("time").sleep(seconds),
        stop_requested=stop,
        poll_interval=timedelta(seconds=1),
        renew_interval=timedelta(seconds=10),
    )


__all__ = (
    "ProductionServiceHooks",
    "ProductionServicesUnavailable",
    "build_production_runtime",
)
