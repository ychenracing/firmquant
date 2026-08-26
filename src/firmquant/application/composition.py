"""Default local composition for safe broker startup and reconciliation."""

from __future__ import annotations

import hashlib
import importlib
import math
from collections.abc import Callable, Collection, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol, cast

from firmquant.application.operations import OperatorCommandDenied, OperatorReconciliation
from firmquant.application.runtime import ReadOnlyBrokerSession
from firmquant.broker.gateway import BrokerGateway
from firmquant.broker.paper import PaperBroker
from firmquant.broker.replay import RecordedReplayBroker
from firmquant.config import Mode, PathSettings, Settings, load_settings
from firmquant.domain.broker_facts import (
    AccountType,
    BrokerAccountFact,
    BrokerSnapshot,
    MarketSessionStatus,
    Side,
)
from firmquant.domain.orders import OrderState
from firmquant.domain.states import RuntimeState, RuntimeStatus
from firmquant.domain.values import Money, Shares, Symbol
from firmquant.execution.policy import ExecutionPolicy, FeeSchedule, FillModel
from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.database import Database
from firmquant.persistence.writer_lease import WriterLease
from firmquant.reconciliation.models import (
    ExpectedPosition,
    OperationalLedgerView,
    OperationalOrderView,
    ReconciliationFacts,
    ReconciliationKind,
    StrategyAccountView,
)
from firmquant.reconciliation.service import ReconciliationService
from firmquant.scheduling.sessions import WorkflowReceiptStore
from firmquant.strategy.identity import StrategyIdentity

_PAPER_ACCOUNT_NAMESPACE = "firmquant-paper-single-account"
_ACCOUNT_FILE = "uquant-account.json"
_REPLAY_FILE = "broker-recording.jsonl"


class _UquantPosition(Protocol):
    shares: int


class _UquantOrder(Protocol):
    order_id: str


class _UquantAccount(Protocol):
    cash: float
    positions: Mapping[str, _UquantPosition]
    pending_orders: Collection[object]
    order_ledger: Collection[_UquantOrder]
    fills: Collection[object]
    data_hash: str
    data_hash_as_of: str
    data_hash_symbols: list[str]
    code_hash: str


class _UquantExecutionConfig(Protocol):
    max_volume_participation: float
    slippage: float
    commission_rate: float
    min_commission: float
    stamp_duty: float
    transfer_fee: float


class _UquantDataManifest(Protocol):
    digest: str
    end: str
    symbols: tuple[str, ...]


class _UquantDataStore(Protocol):
    def manifest(self, symbols: tuple[str, ...], *, as_of: str) -> _UquantDataManifest: ...


class _LoadAccount(Protocol):
    def __call__(
        self,
        path: Path,
        *,
        require_hashes: bool,
        allow_legacy_schema: bool,
    ) -> object: ...


class _DataStoreFactory(Protocol):
    def __call__(self, root: Path) -> _UquantDataStore: ...


def _uquant_symbol(module_name: str, symbol: str) -> object:
    """Load a locked uquant symbol without trusting its untyped package boundary."""

    try:
        module = importlib.import_module(module_name)
        return cast(object, getattr(module, symbol))
    except (AttributeError, ImportError) as error:
        raise OperatorCommandDenied("UQUANT_CONTRACT_UNAVAILABLE") from error


def _uquant_account_contract() -> tuple[
    type[object],
    _LoadAccount,
    Callable[[object], str],
]:
    account_type = _uquant_symbol("uquant.types", "AccountState")
    loader = _uquant_symbol("uquant.account", "load_account")
    economic_hash = _uquant_symbol("uquant.account", "economic_state_sha256")
    if not isinstance(account_type, type) or not callable(loader) or not callable(economic_hash):
        raise OperatorCommandDenied("UQUANT_CONTRACT_INVALID")
    return (
        cast(type[object], account_type),
        cast(_LoadAccount, loader),
        cast(Callable[[object], str], economic_hash),
    )


def _uquant_execution_config() -> _UquantExecutionConfig:
    config = _uquant_symbol("uquant.config", "DEFAULT_CONFIG")
    required = (
        "max_volume_participation",
        "slippage",
        "commission_rate",
        "min_commission",
        "stamp_duty",
        "transfer_fee",
    )
    if any(not hasattr(config, field) for field in required):
        raise OperatorCommandDenied("UQUANT_CONTRACT_INVALID")
    return cast(_UquantExecutionConfig, config)


def _uquant_data_manifest(
    data_directory: Path,
    symbols: tuple[str, ...],
    *,
    as_of: str,
) -> _UquantDataManifest:
    factory = _uquant_symbol("uquant.data", "DataStore")
    if not callable(factory):
        raise OperatorCommandDenied("UQUANT_CONTRACT_INVALID")
    manifest = cast(_DataStoreFactory, factory)(data_directory).manifest(symbols, as_of=as_of)
    if any(not hasattr(manifest, field) for field in ("digest", "end", "symbols")):
        raise OperatorCommandDenied("UQUANT_CONTRACT_INVALID")
    return manifest


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _money(value: object, *, label: str) -> Money:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise OperatorCommandDenied("UQUANT_ACCOUNT_STATE_INVALID")
    if isinstance(value, float) and not math.isfinite(value):
        raise OperatorCommandDenied("UQUANT_ACCOUNT_STATE_INVALID")
    try:
        observed = Decimal(str(value))
    except InvalidOperation as error:
        raise OperatorCommandDenied("UQUANT_ACCOUNT_STATE_INVALID") from error
    if not observed.is_finite() or observed < 0:
        raise OperatorCommandDenied("UQUANT_ACCOUNT_STATE_INVALID")
    try:
        return Money(observed)
    except Exception as error:
        raise OperatorCommandDenied(f"{label}_PRECISION_INVALID") from error


def _paper_policy() -> ExecutionPolicy:
    """Read every economic simulation input from the locked uquant config."""

    config = _uquant_execution_config()
    return ExecutionPolicy(
        fill_model=FillModel(
            max_volume_participation=Decimal(str(config.max_volume_participation)),
            slippage_bps=Decimal(str(config.slippage)) * Decimal("10000"),
        ),
        fee_schedule=FeeSchedule(
            commission_rate=Decimal(str(config.commission_rate)),
            minimum_commission=Decimal(str(config.min_commission)),
            stamp_duty_rate=Decimal(str(config.stamp_duty)),
            transfer_fee_rate=Decimal(str(config.transfer_fee)),
            fee_quantum=Decimal("0.0001"),
        ),
        allow_auction=False,
    )


def _safe_account(path: Path) -> _UquantAccount:
    if path.is_symlink() or not path.is_file():
        raise OperatorCommandDenied("UQUANT_ACCOUNT_STATE_UNAVAILABLE")
    account_type, load_account, _ = _uquant_account_contract()
    try:
        account = load_account(path, require_hashes=True, allow_legacy_schema=False)
    except Exception as error:
        raise OperatorCommandDenied("UQUANT_ACCOUNT_STATE_INVALID") from error
    if not isinstance(account, account_type):
        raise OperatorCommandDenied("UQUANT_ACCOUNT_STATE_INVALID")
    return cast(_UquantAccount, account)


def _paper_gateway(
    *,
    settings: Settings,
    account: _UquantAccount,
    clock: Callable[[], datetime],
) -> PaperBroker:
    if account.positions or account.pending_orders or account.order_ledger or account.fills:
        raise OperatorCommandDenied("PAPER_BROKER_SEED_REQUIRED")
    cash = _money(account.cash, label="PAPER_CASH")
    alias = settings.broker.account_alias or _PAPER_ACCOUNT_NAMESPACE
    account_id_hash = hashlib.sha256(f"paper:{alias}".encode()).hexdigest()
    return PaperBroker(
        account=BrokerAccountFact(
            account_id_hash=account_id_hash,
            account_type=AccountType.CASH,
            available_cash=cash,
            total_assets=cash,
        ),
        positions=(),
        instruments=(),
        quotes=(),
        market_status=MarketSessionStatus.CLOSED,
        policy=_paper_policy(),
        clock=clock,
    )


def _snapshot_stable_values(snapshot: BrokerSnapshot) -> tuple[object, ...]:
    return (
        snapshot.account.account_id_hash,
        snapshot.account.account_type.value,
        snapshot.session_date.isoformat(),
        snapshot.captured_at.isoformat(),
        snapshot.broker_event_watermark,
        snapshot.raw_payload_sha256,
        1,
    )


def _persist_snapshot(database: Database, snapshot: BrokerSnapshot) -> None:
    parent = _snapshot_stable_values(snapshot)
    cash = (
        snapshot.account.available_cash.canonical,
        snapshot.account.total_assets.canonical,
    )
    positions = tuple(
        (
            position.symbol.canonical,
            position.total_shares.value,
            position.sellable_shares.value,
            None if position.average_cost is None else position.average_cost.canonical,
            position.market_value.canonical,
        )
        for position in sorted(snapshot.positions, key=lambda item: item.symbol.canonical)
    )
    with database.transaction():
        existing = database.query_one(
            "SELECT account_id_hash, account_type, session_date, captured_at, "
            "broker_event_watermark, raw_payload_sha256, complete "
            "FROM broker_snapshots WHERE snapshot_id = ?",
            (snapshot.snapshot_id,),
        )
        if existing is not None:
            stored_cash = database.query_one(
                "SELECT available_cash, total_assets FROM cash_snapshots WHERE snapshot_id = ?",
                (snapshot.snapshot_id,),
            )
            stored_positions = database.query_all(
                "SELECT symbol, total_shares, sellable_shares, average_cost, market_value "
                "FROM position_snapshots WHERE snapshot_id = ? ORDER BY symbol",
                (snapshot.snapshot_id,),
            )
            if (
                tuple(existing) != parent
                or stored_cash is None
                or tuple(stored_cash) != cash
                or tuple(tuple(row) for row in stored_positions) != positions
            ):
                raise OperatorCommandDenied("BROKER_SNAPSHOT_IDENTITY_COLLISION")
            return
        database.write(
            """
            INSERT INTO broker_snapshots(
                snapshot_id, account_id_hash, account_type, session_date, captured_at,
                broker_event_watermark, raw_payload_sha256, complete
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (snapshot.snapshot_id, *parent),
        )
        database.write(
            "INSERT INTO cash_snapshots(snapshot_id, available_cash, total_assets) VALUES (?, ?, ?)",
            (snapshot.snapshot_id, *cash),
        )
        for position in positions:
            database.write(
                """
                INSERT INTO position_snapshots(
                    snapshot_id, symbol, total_shares, sellable_shares,
                    average_cost, market_value
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (snapshot.snapshot_id, *position),
            )
        AuditLedger(database).append(
            audit_event_id="broker-snapshot:" + snapshot.raw_payload_sha256,
            category="BROKER_SNAPSHOT",
            actor="runtime-composition",
            payload={
                "schema": "firmquant.broker-snapshot-receipt.v1",
                "snapshot_id": snapshot.snapshot_id,
                "raw_payload_sha256": snapshot.raw_payload_sha256,
                "session_date": snapshot.session_date,
                "broker_event_watermark": snapshot.broker_event_watermark,
                "position_count": len(snapshot.positions),
                "order_count": len(snapshot.orders),
                "fill_count": len(snapshot.fills),
            },
            created_at=snapshot.captured_at,
        )


def _previous_account_identity(
    database: Database,
    snapshot: BrokerSnapshot,
) -> tuple[str, AccountType]:
    row = database.query_one(
        "SELECT account_id_hash, account_type FROM broker_snapshots "
        "ORDER BY captured_at DESC, snapshot_id DESC LIMIT 1"
    )
    if row is None:
        return snapshot.account.account_id_hash, snapshot.account.account_type
    try:
        return str(row["account_id_hash"]), AccountType(str(row["account_type"]))
    except ValueError as error:
        raise OperatorCommandDenied("BROKER_ACCOUNT_BINDING_INVALID") from error


def _strategy_account(account: _UquantAccount, snapshot: BrokerSnapshot) -> StrategyAccountView:
    broker_positions = {item.symbol: item for item in snapshot.positions}
    positions: list[ExpectedPosition] = []
    marked_value = Decimal(0)
    for raw_symbol, position in sorted(account.positions.items()):
        symbol = Symbol.parse(raw_symbol)
        shares = Shares(position.shares)
        if not shares.is_positive:
            raise OperatorCommandDenied("UQUANT_ACCOUNT_STATE_INVALID")
        broker = broker_positions.get(symbol)
        sellable = Shares(0) if broker is None else broker.sellable_shares
        if broker is not None and broker.total_shares == shares:
            marked_value += broker.market_value.value
        positions.append(
            ExpectedPosition(
                symbol=symbol,
                total_shares=shares,
                sellable_shares=sellable,
            )
        )
    cash = _money(account.cash, label="UQUANT_CASH")
    total_assets = Money(cash.value + marked_value)
    _, _, economic_state_sha256 = _uquant_account_contract()
    try:
        economic_sha256 = economic_state_sha256(account)
    except Exception as error:
        raise OperatorCommandDenied("UQUANT_ACCOUNT_STATE_INVALID") from error
    known_ids = frozenset(order.order_id for order in account.order_ledger)
    return StrategyAccountView(
        available_cash=cash,
        total_assets=total_assets,
        positions=tuple(positions),
        known_uquant_order_ids=known_ids,
        economic_state_sha256=economic_sha256,
    )


def _operational_ledger(
    database: Database,
    *,
    expected_account_id_hash: str,
    expected_account_type: AccountType,
) -> OperationalLedgerView:
    rows = database.query_all(
        """
        SELECT b.broker_order_id, i.uquant_order_id, i.symbol, i.side,
               i.requested_shares, i.filled_shares, i.state
        FROM broker_orders b
        JOIN execution_intents i ON i.execution_id = b.execution_id
        WHERE b.ownership = 'SYSTEM'
        ORDER BY b.broker_order_id
        """
    )
    orders = tuple(
        OperationalOrderView(
            broker_order_id=str(row["broker_order_id"]),
            uquant_order_id=str(row["uquant_order_id"]),
            symbol=Symbol.parse(str(row["symbol"])),
            side=Side(str(row["side"])),
            requested_shares=Shares(int(row["requested_shares"])),
            filled_shares=Shares(int(row["filled_shares"])),
            local_state=OrderState(str(row["state"])),
        )
        for row in rows
    )
    fill_rows = database.query_all("SELECT broker_fill_id FROM fills ORDER BY broker_fill_id")
    unresolved_rows = database.query_all(
        "SELECT execution_id FROM execution_intents WHERE state = 'UNKNOWN' ORDER BY execution_id"
    )
    submitting_rows = database.query_all(
        "SELECT execution_id FROM execution_intents WHERE state = 'SUBMITTING' ORDER BY execution_id"
    )
    return OperationalLedgerView(
        expected_account_id_hash=expected_account_id_hash,
        expected_account_type=expected_account_type,
        orders=orders,
        known_broker_fill_ids=frozenset(str(row["broker_fill_id"]) for row in fill_rows),
        unresolved_execution_ids=tuple(str(row["execution_id"]) for row in unresolved_rows),
        submitting_unresolved_execution_ids=tuple(str(row["execution_id"]) for row in submitting_rows),
    )


def _data_identity_matches(account: _UquantAccount, data_directory: Path) -> bool:
    try:
        if not account.data_hash or not account.data_hash_as_of or not account.data_hash_symbols:
            return False
        manifest = _uquant_data_manifest(
            data_directory,
            tuple(account.data_hash_symbols),
            as_of=account.data_hash_as_of,
        )
    except Exception:
        return False
    return (
        manifest.digest == account.data_hash
        and manifest.end == account.data_hash_as_of
        and manifest.symbols == tuple(account.data_hash_symbols)
    )


def _configuration_identity_matches(database: Database, configuration_sha256: str) -> bool:
    row = database.query_one(
        "SELECT config_sha256 FROM arm_leases WHERE revoked_at IS NULL "
        "ORDER BY issued_at DESC, lease_id DESC LIMIT 1"
    )
    return row is None or str(row["config_sha256"]) == configuration_sha256


class ConfiguredOperatorPorts:
    """Lazy mode-specific ports used by the installed CLI entry point."""

    def __init__(
        self,
        *,
        config_path: Path,
        clock: Callable[[], datetime] = _now_utc,
    ) -> None:
        self._config_path = Path(config_path)
        self._clock = clock

    def _settings(self) -> Settings:
        settings = load_settings(self._config_path)

        def resolved(path: Path) -> Path:
            return path if path.is_absolute() else self._config_path.parent / path

        return settings.model_copy(
            update={
                "paths": PathSettings(
                    state_directory=resolved(settings.paths.state_directory),
                    data_directory=resolved(settings.paths.data_directory),
                    report_directory=resolved(settings.paths.report_directory),
                    backup_directory=resolved(settings.paths.backup_directory),
                )
            }
        )

    def _configuration_sha256(self) -> str:
        try:
            return hashlib.sha256(self._config_path.read_bytes()).hexdigest()
        except OSError as error:
            raise OperatorCommandDenied("CONFIGURATION_UNAVAILABLE") from error

    @staticmethod
    def _account_path(settings: Settings) -> Path:
        return settings.paths.state_directory / _ACCOUNT_FILE

    def _gateway(self, settings: Settings, account: _UquantAccount) -> BrokerGateway:
        if settings.mode is Mode.PAPER:
            return _paper_gateway(settings=settings, account=account, clock=self._clock)
        if settings.mode is Mode.REPLAY:
            recording = settings.paths.data_directory / _REPLAY_FILE
            if recording.is_symlink() or not recording.is_file():
                raise OperatorCommandDenied("REPLAY_RECORDING_UNAVAILABLE")
            return RecordedReplayBroker.from_jsonl(recording)
        raise OperatorCommandDenied("XTQUANT_RUNTIME_PREREQUISITES_UNAVAILABLE")

    def doctor_broker(self) -> BrokerGateway:
        """Build a fresh read-only diagnostic gateway without write capability."""

        identity = StrategyIdentity.locked()
        try:
            identity.verify()
        except Exception as error:
            raise OperatorCommandDenied("UQUANT_IDENTITY_UNAVAILABLE") from error
        settings = self._settings()
        account = _safe_account(self._account_path(settings))
        return self._gateway(settings, account)

    def cancel_system_orders(self, broker_order_ids: tuple[str, ...]) -> tuple[str, ...]:
        """Expose cancellation only when a recoverable mode-specific writer exists."""

        if (
            not isinstance(broker_order_ids, tuple)
            or len(set(broker_order_ids)) != len(broker_order_ids)
            or any(not isinstance(item, str) or not item for item in broker_order_ids)
        ):
            raise OperatorCommandDenied("CANCEL_REQUEST_INVALID")
        if not broker_order_ids:
            return ()
        mode = self._settings().mode
        if mode is Mode.PAPER:
            raise OperatorCommandDenied("PAPER_BROKER_REATTACH_REQUIRED")
        if mode in {Mode.REPLAY, Mode.SHADOW}:
            raise OperatorCommandDenied("MODE_NOT_WRITE_CAPABLE")
        raise OperatorCommandDenied("WRITE_CAPABILITY_UNAVAILABLE")

    def _run_reconciliation(
        self,
        database: Database,
        *,
        settings: Settings,
        kind: ReconciliationKind,
    ) -> OperatorReconciliation:
        identity = StrategyIdentity.locked()
        try:
            identity.verify()
        except Exception as error:
            raise OperatorCommandDenied("UQUANT_IDENTITY_UNAVAILABLE") from error
        account = _safe_account(self._account_path(settings))
        gateway = self._gateway(settings, account)
        gateway.connect()
        try:
            snapshot = ReadOnlyBrokerSession(
                gateway=gateway,
                clock=self._clock,
            ).capture_snapshot()
            expected_id, expected_type = _previous_account_identity(database, snapshot)
            _persist_snapshot(database, snapshot)
            facts = ReconciliationFacts(
                broker_snapshot=snapshot,
                strategy_account=_strategy_account(account, snapshot),
                operational_ledger=_operational_ledger(
                    database,
                    expected_account_id_hash=expected_id,
                    expected_account_type=expected_type,
                ),
                company_action_suspected_symbols=frozenset(),
                uquant_code_identity_matches=account.code_hash == identity.economic_code_fingerprint,
                data_identity_matches=_data_identity_matches(
                    account,
                    settings.paths.data_directory,
                ),
                config_identity_matches=_configuration_identity_matches(
                    database,
                    self._configuration_sha256(),
                ),
            )
            receipt = ReconciliationService(
                database=database,
                cash_tolerance=Money(Decimal("0.01")),
                clock=self._clock,
            ).run(kind, facts)
        finally:
            gateway.disconnect()
        return OperatorReconciliation(
            reconciliation_id=receipt.reconciliation_id,
            passed=receipt.passed,
            blockers=receipt.blockers,
        )

    def reconcile(self, database: Database) -> OperatorReconciliation:
        return self._run_reconciliation(
            database,
            settings=self._settings(),
            kind=ReconciliationKind.MANUAL,
        )

    @staticmethod
    def _transition(
        receipts: WorkflowReceiptStore,
        *,
        mode: Mode,
        previous: RuntimeStatus,
        target: RuntimeState,
        reason: str,
        blockers: tuple[str, ...] = (),
        at: datetime,
    ) -> RuntimeStatus:
        current = previous.transition(target, reason=reason, blockers=blockers)
        receipts.save_runtime(
            mode=mode,
            previous=previous,
            current=current,
            created_at=at,
        )
        return current

    def run(self, mode: Mode) -> Mapping[str, object]:
        settings = self._settings()
        if mode is not settings.mode:
            raise OperatorCommandDenied("RUN_MODE_CONFIG_MISMATCH")
        database_path = settings.paths.state_directory / "firmquant.sqlite3"
        with WriterLease.acquire(
            database_path,
            owner="configured-runtime",
            clock=self._clock,
        ) as writer:
            receipts = WorkflowReceiptStore(writer_lease=writer)
            status = receipts.load_runtime(mode)
            if status.state in {RuntimeState.HALTED, RuntimeState.STOPPING}:
                raise OperatorCommandDenied("EXPLICIT_RESUME_REQUIRED")
            if status.state is RuntimeState.EXECUTING:
                raise OperatorCommandDenied("RUNTIME_ALREADY_EXECUTING")
            if status.state is RuntimeState.DISARMED:
                status = self._transition(
                    receipts,
                    mode=mode,
                    previous=status,
                    target=RuntimeState.STARTING,
                    reason="configured runtime startup requested",
                    at=self._clock(),
                )
            if status.state is not RuntimeState.RECONCILING:
                status = self._transition(
                    receipts,
                    mode=mode,
                    previous=status,
                    target=RuntimeState.RECONCILING,
                    reason="configured runtime startup reconciliation",
                    at=self._clock(),
                )
            try:
                reconciliation = self._run_reconciliation(
                    writer.database,
                    settings=settings,
                    kind=ReconciliationKind.STARTUP,
                )
            except Exception:
                self._transition(
                    receipts,
                    mode=mode,
                    previous=status,
                    target=RuntimeState.HALTED,
                    reason="configured runtime startup failed closed",
                    blockers=("STARTUP_RECONCILIATION_EXCEPTION",),
                    at=self._clock(),
                )
                raise
            if not reconciliation.passed:
                self._transition(
                    receipts,
                    mode=mode,
                    previous=status,
                    target=RuntimeState.HALTED,
                    reason="configured runtime startup reconciliation blocked READY",
                    blockers=reconciliation.blockers,
                    at=self._clock(),
                )
                raise OperatorCommandDenied("STARTUP_RECONCILIATION_FAILED")
            status = self._transition(
                receipts,
                mode=mode,
                previous=status,
                target=RuntimeState.READY,
                reason="configured runtime startup reconciliation passed",
                at=self._clock(),
            )
        return {
            "mode": mode.value,
            "runtime_state": status.state.value,
            "reconciliation_id": reconciliation.reconciliation_id,
            "reconciliation_passed": reconciliation.passed,
            "blockers": list(reconciliation.blockers),
            "real_order_calls": 0,
        }


def compose_operator_ports(config_path: Path) -> ConfiguredOperatorPorts:
    return ConfiguredOperatorPorts(config_path=config_path)


__all__ = ("ConfiguredOperatorPorts", "compose_operator_ports")
