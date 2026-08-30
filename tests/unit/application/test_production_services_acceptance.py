from __future__ import annotations

import hashlib
import sys
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import firmquant.application.production_services as ps
from firmquant.application.close_checkpoint import CloseStep
from firmquant.application.execution_evidence import (
    BlockerCode,
    EvidenceIdentity,
    EvidenceStage,
    ExecutionObservation,
    OrderObservation,
    PositionObservation,
    TargetObservation,
)
from firmquant.application.production_events import ProductionEventJournal
from firmquant.application.production_identity import (
    DeploymentIdentity,
    OperationalEvidenceIdentity,
)
from firmquant.application.production_services import (
    ProductionServiceHooks,
    ProductionServicesUnavailable,
)
from firmquant.broker.fake import FakeBroker
from firmquant.broker.normalization import normalize_broker_event
from firmquant.broker.xtquant_safety import XtQuantSafetyManifest
from firmquant.config import (
    BrokerAdapter,
    BrokerSettings,
    ComplianceSettings,
    DeploymentCaps,
    Mode,
    PathSettings,
    Settings,
)
from firmquant.domain.broker_facts import BrokerSnapshot, MarketSessionStatus
from firmquant.domain.states import RuntimeState
from firmquant.domain.values import Money, Shares, Symbol
from firmquant.execution.planner import ExecutionPlanner
from firmquant.market_data.calendar import AuthoritativeTradingCalendar
from firmquant.market_data.xtquant_daily import DailyDataUpdateReceipt
from firmquant.persistence.writer_lease import WriterLease
from firmquant.reconciliation.models import ReconciliationKind
from firmquant.strategy.snapshots import DecisionSnapshot
from tests.fixtures.session_cases import (
    BUY_SYMBOL,
    NOW,
    STRATEGY_SESSION,
    decision_snapshot,
    execution_snapshot,
)

EXECUTION_SESSION = date(2026, 8, 25)
POST_CLOSE = datetime(2026, 8, 25, 7, 10, tzinfo=UTC)


class AccountPosition:
    def __init__(self, shares: int) -> None:
        self.shares = shares

    def sellable_shares(self, _date: str) -> int:
        return self.shares


class Account:
    def __init__(self) -> None:
        self.payload: dict[str, object] = {
            "cash": 1000.0,
            "positions": {
                "sz300308": {
                    "shares": 1000,
                }
            },
            "order_ledger": [],
            "data_hash": "d" * 64,
            "data_hash_as_of": "2026-08-24",
            "data_hash_symbols": ["sz300308"],
            "code_hash": "",
            "capital_peak": 11000.0,
        }

    @property
    def cash(self) -> float:
        return float(self.payload["cash"])

    @property
    def positions(self) -> dict[str, AccountPosition]:
        raw = self.payload["positions"]
        assert isinstance(raw, dict)
        return {
            str(symbol): AccountPosition(int(position["shares"]))
            for symbol, position in raw.items()
            if isinstance(position, dict)
        }

    @property
    def order_ledger(self) -> list[object]:
        return []

    @property
    def fills(self) -> list[object]:
        return []

    def to_dict(self) -> dict[str, object]:
        return self.payload


class AccountStore:
    def hash_state(self, _state: object) -> str:
        return "c" * 64

    def hash_file(self, _path: Path) -> str:
        return "c" * 64

    def save(self, _state: object, _path: Path) -> None:
        return None


class Accounts:
    def __init__(self, root: Path, account: Account | None = None) -> None:
        self.path = root / "uquant-account.json"
        self.store = AccountStore()
        self.account = account or Account()
        self.persisted: list[tuple[str, str, str]] = []
        self.persist_result = "e" * 64

    def load(self):
        return self.account

    def sync_broker_snapshot(self, snapshot):
        return self.account, SimpleNamespace(
            account_before_sha256="c" * 64,
            account_after_sha256="c" * 64,
            snapshot_id=snapshot.snapshot_id,
        )

    def prepare_broker_snapshot(self, snapshot):
        return SimpleNamespace(
            prepared_account=self.account,
            receipt=SimpleNamespace(snapshot_id=snapshot.snapshot_id),
            account_before_sha256="c" * 64,
            account_after_sha256="c" * 64,
            evidence_sha256=snapshot.raw_payload_sha256,
        )

    def commit_broker_snapshot(self, prepared) -> str:
        return str(prepared.account_after_sha256)

    def persist_prepared(
        self,
        _account,
        *,
        expected_before_sha256: str,
        operation_kind: str,
        evidence_sha256: str,
    ) -> str:
        self.persisted.append((expected_before_sha256, operation_kind, evidence_sha256))
        return self.persist_result


class DataUpdater:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], date]] = []

    def update(self, symbols: tuple[str, ...], *, through: date):
        self.calls.append((symbols, through))
        return DailyDataUpdateReceipt(
            latest_common_session=through,
            manifest_sha256="d" * 64,
            appended_rows=1,
            symbols=tuple(sorted(symbols)),
        )


class Strategy:
    def __init__(self, decision: DecisionSnapshot) -> None:
        self.decision = decision
        self.requests: list[object] = []

    def decide_once(self, request):
        self.requests.append(request)
        return self.decision

    def recover_existing_decision(self, request, snapshot):
        self.requests.append(request)
        return snapshot


class Universe:
    deployment_symbols = ("sz300308", "sz300502")

    def allowed(self, symbol: str, _as_of: date) -> bool:
        return symbol in self.deployment_symbols


class PassingReconciler:
    def __init__(self) -> None:
        self.facts: list[object] = []

    def evaluate(self, kind, facts):
        self.facts.append(facts)
        return SimpleNamespace(
            reconciliation_id="recon_" + "a" * 64,
            kind=kind,
            passed=True,
            blockers=(),
        )

    def commit(self, _receipt, *, broker_snapshot_sha256):
        assert len(broker_snapshot_sha256) == 64


class RecoveryResult:
    def __init__(
        self,
        *,
        halt_required: bool = False,
        blockers: tuple[str, ...] = (),
        unresolved: tuple[str, ...] = (),
    ) -> None:
        self.halt_required = halt_required
        self.blockers = blockers
        self.unresolved_order_ids = unresolved


class RecoveryService:
    result = RecoveryResult()

    def __init__(self, **_kwargs: object) -> None:
        pass

    def recover(self):
        return type(self).result


def deployment_caps() -> DeploymentCaps:
    return DeploymentCaps(
        max_order_notional=Decimal("10000"),
        max_daily_submitted_notional=Decimal("30000"),
        max_daily_filled_notional=Decimal("30000"),
        max_symbol_notional=Decimal("20000"),
        max_total_gross_notional=Decimal("50000"),
    )


def settings_for(root: Path, mode: Mode) -> tuple[Settings, Path]:
    for name in ("state", "data", "reports", "backups", "source", "userdata"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "data" / "sz300308.csv").write_text(
        "date,open,high,low,close,volume,amount\n2026-08-24,10,10,10,10,1000,10000\n",
        encoding="utf-8",
    )
    safety = root / "safety.json"
    safety.write_text("{}", encoding="utf-8")
    config = root / "firmquant.toml"
    config.write_text("candidate = true\n", encoding="utf-8")
    live = mode in {Mode.CANARY, Mode.LIVE}
    return (
        Settings(
            mode=mode,
            live_trading_enabled=live,
            broker=BrokerSettings(
                adapter=BrokerAdapter.XTQUANT,
                account_alias="account-test",
                xtquant_userdata_path=root / "userdata",
                session_id=123456,
                safety_manifest_path=safety,
            ),
            paths=PathSettings(
                state_directory=root / "state",
                data_directory=root / "data",
                report_directory=root / "reports",
                backup_directory=root / "backups",
                uquant_source_checkout=root / "source",
            ),
            compliance=ComplianceSettings(
                program_trading_report_confirmed=live,
                broker_api_authorized=live,
            ),
            canary_caps=deployment_caps() if mode is Mode.CANARY else None,
            live_caps=deployment_caps() if mode is Mode.LIVE else None,
        ),
        config,
    )


def safety_manifest() -> XtQuantSafetyManifest:
    return XtQuantSafetyManifest(
        source_name="reviewed-test",
        source_sha256="a" * 64,
        probe_symbol=Symbol.parse("sz300308"),
        equity_product_types=frozenset({1}),
        trading_instrument_statuses=frozenset({1}),
        open_stock_statuses=frozenset({1}),
        auction_stock_statuses=frozenset({2}),
        break_stock_statuses=frozenset({3}),
        closed_stock_statuses=frozenset({4}),
        trading_units={"SH": 100, "SZ": 100, "BJ": 100},
        volume_multipliers={"SH": 1, "SZ": 1, "BJ": 1},
        commission_rate=Decimal("0.0003"),
        minimum_commission=Decimal("5"),
        stamp_duty_rate=Decimal("0.0005"),
        transfer_fee_rate=Decimal("0.00001"),
    )


def calendar() -> AuthoritativeTradingCalendar:
    return AuthoritativeTradingCalendar(
        source="test-calendar",
        source_sha256="b" * 64,
        covered_from=date(2026, 8, 20),
        covered_through=date(2026, 8, 28),
        trading_sessions=(
            date(2026, 8, 21),
            date(2026, 8, 24),
            date(2026, 8, 25),
            date(2026, 8, 26),
            date(2026, 8, 27),
            date(2026, 8, 28),
        ),
    )


def broker_for(*, status: MarketSessionStatus = MarketSessionStatus.OPEN) -> FakeBroker:
    facts = execution_snapshot()
    snapshot = facts.broker_snapshot
    broker = FakeBroker(
        account=snapshot.account,
        positions=snapshot.positions,
        orders=snapshot.orders,
        fills=snapshot.fills,
        instruments=facts.instruments,
        quotes=facts.quotes,
        market_status=status,
        clock=lambda: NOW,
    )
    broker.connect()
    return broker


@contextmanager
def hook_case(
    root: Path,
    *,
    mode: Mode = Mode.SHADOW,
    status: MarketSessionStatus = MarketSessionStatus.OPEN,
    clock=lambda: NOW,
    operational_identity_provider=None,
    promotion_identity_provider=None,
):
    settings, config = settings_for(root, mode)
    identity = ps._RuntimeIdentity(
        firmquant_commit="f" * 40,
        uquant_commit="1" * 40,
        config_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
        promotion_config_sha256="c" * 64,
        safety_manifest_sha256="s" * 64,
    )
    account_repository = Accounts(root)
    broker = broker_for(status=status)
    with WriterLease.acquire(
        root / "state" / "firmquant.sqlite3",
        owner="production-service-test",
        clock=clock,
    ) as writer:
        hooks = ProductionServiceHooks(
            config_path=config,
            settings=settings,
            writer=writer,
            broker=broker,
            calendar=calendar(),
            account_repository=account_repository,
            data_updater=DataUpdater(),
            strategy_adapter=Strategy(decision_snapshot()),
            universe_policy=Universe(),
            event_journal=ProductionEventJournal(writer.database),
            identity=identity,
            safety_manifest=safety_manifest(),
            clock=clock,
            operational_identity_provider=operational_identity_provider,
            promotion_identity_provider=promotion_identity_provider,
        )
        try:
            yield hooks, writer, broker, account_repository
        finally:
            broker.disconnect()


def ready(hooks: ProductionServiceHooks) -> None:
    hooks._transition(RuntimeState.STARTING, reason="test startup")
    hooks._transition(RuntimeState.RECONCILING, reason="test reconciliation")
    hooks._transition(RuntimeState.READY, reason="test ready")


def complete_close(hooks: ProductionServiceHooks, session: date = STRATEGY_SESSION) -> None:
    for step in CloseStep:
        hooks._close.append(
            session,
            step,
            evidence={"step": step.value},
            created_at=NOW,
        )


def test_helpers_fail_closed_and_normalize_authority_facts(tmp_path: Path) -> None:
    assert ps._fraction(Decimal("5"), Decimal("10")) == Decimal("0.5")
    assert ps._fraction(Decimal("1"), Decimal("0")) == 0
    assert ps._fraction(Decimal("20"), Decimal("10")) == 1
    assert ps._count(None, label="X") == 0
    assert ps._count(3, label="X") == 3
    with pytest.raises(ProductionServicesUnavailable, match="X_INVALID"):
        ps._count(True, label="X")

    account = Account()
    assert ps._account_payload(account)["cash"] == 1000.0
    with pytest.raises(ProductionServicesUnavailable, match="CONTRACT"):
        ps._account_payload(object())

    class BadPayload:
        def to_dict(self):
            return []

    with pytest.raises(ProductionServicesUnavailable, match="PAYLOAD"):
        ps._account_payload(BadPayload())

    repository = Accounts(tmp_path)
    view = ps._strategy_view(
        account,
        execution_snapshot().broker_snapshot.positions,
        repository,
    )
    assert view.available_cash == Money(Decimal("1000"))
    assert view.total_assets == Money(Decimal("11000"))
    assert view.positions[0].total_shares == Shares(1000)
    assert view.economic_state_sha256 == "c" * 64

    account.payload["cash"] = True
    with pytest.raises(ProductionServicesUnavailable, match="CASH"):
        ps._strategy_view(account, (), repository)

    assert ps._fee_schedule(safety_manifest()).minimum_commission == Decimal("5")
    symbols = ps._decision_symbols(decision_snapshot())
    assert BUY_SYMBOL in symbols
    with pytest.raises(ProductionServicesUnavailable, match="ORDER_PAYLOAD"):
        ps._decision_symbols(SimpleNamespace(uquant_payload={"orders": "bad"}))


def test_load_engine_is_source_bound_and_detects_module_collision(tmp_path: Path) -> None:
    source = tmp_path / "source"
    package = source / "uquant"
    package.mkdir(parents=True)
    engine_path = package / "engine.py"
    engine_path.write_text(
        "DEFAULT_CONFIG = object()\n"
        "class ProductionEngine:\n"
        "    def __init__(self, data_dir, config):\n"
        "        self.data_dir = data_dir\n"
        "        self.config = config\n"
        "    def decide(self, account, request):\n"
        "        return None\n",
        encoding="utf-8",
    )
    sys.modules.pop("firmquant_verified_uquant_engine", None)
    engine = ps._load_engine(source, tmp_path / "data")
    assert engine.data_dir == tmp_path / "data"
    assert ps._load_engine(source, tmp_path / "other").data_dir == tmp_path / "other"

    other = tmp_path / "other-source"
    (other / "uquant").mkdir(parents=True)
    (other / "uquant" / "engine.py").write_text(
        engine_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    with pytest.raises(ProductionServicesUnavailable, match="COLLISION"):
        ps._load_engine(other, tmp_path / "data")
    sys.modules.pop("firmquant_verified_uquant_engine", None)


def test_hook_reconciliation_builds_session_scoped_authority_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with hook_case(tmp_path) as (hooks, writer, _broker, accounts):
        from firmquant.persistence.account_authority import AccountBinding, AccountBindingRepository
        from firmquant.strategy.identity import StrategyIdentity

        broker_snapshot = execution_snapshot().broker_snapshot
        identity = StrategyIdentity.locked()
        AccountBindingRepository(writer.database).bind(
            AccountBinding.create(
                account_id_hash=broker_snapshot.account.account_id_hash,
                account_type=broker_snapshot.account.account_type,
                broker_snapshot_sha256="a" * 64,
                account_state_sha256="c" * 64,
                uquant_commit=identity.uquant_commit,
                uquant_code_fingerprint=identity.economic_code_fingerprint,
                data_hash="d" * 64,
                data_as_of="2026-08-24",
                data_symbols=("sz300308",),
                created_at=NOW,
            )
        )
        reconciler = PassingReconciler()
        hooks._reconciler = reconciler
        monkeypatch.setattr(ps, "_data_identity_matches", lambda *_args: True)
        monkeypatch.setattr(
            ps,
            "configuration_sha256",
            lambda _path: hooks._identity.config_sha256,
        )
        receipt, snapshot, account = hooks._reconcile(ReconciliationKind.STARTUP)

        assert receipt.passed is True
        assert snapshot.session_date == EXECUTION_SESSION
        assert account is accounts.account
        facts = reconciler.facts[0]
        assert facts.operational_ledger.expected_account_id_hash == snapshot.account.account_id_hash
        assert facts.operational_ledger.orders == ()
        assert facts.data_identity_matches is True
        assert facts.config_identity_matches is True

        hooks._reconciler = SimpleNamespace(
            evaluate=lambda _kind, _facts: SimpleNamespace(
                passed=False,
                blockers=("BROKER_MISMATCH",),
            ),
            commit=lambda _receipt, **_kwargs: None,
        )
        with pytest.raises(ProductionServicesUnavailable, match="BROKER_MISMATCH"):
            hooks._reconcile(ReconciliationKind.MANUAL)


def test_startup_requires_recovery_then_smoke_reconciliation_and_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ps, "ProductionRecoveryService", RecoveryService)
    monkeypatch.setattr(ps, "run_readonly_production_smoke", lambda **_kwargs: object())
    RecoveryService.result = RecoveryResult()

    with hook_case(tmp_path / "ok") as (hooks, _writer, _broker, _accounts):
        receipt = SimpleNamespace(reconciliation_id="recon_" + "b" * 64)
        monkeypatch.setattr(
            hooks,
            "_reconcile",
            lambda _kind: (receipt, execution_snapshot().broker_snapshot, Account()),
        )
        reconciliation_id = hooks.startup()
        assert reconciliation_id == receipt.reconciliation_id
        assert hooks.status.state is RuntimeState.READY
        assert hooks._startup_reconciliation_id == receipt.reconciliation_id

    RecoveryService.result = RecoveryResult(
        halt_required=True,
        blockers=("UNKNOWN_ORDER",),
        unresolved=("execution-1",),
    )
    with hook_case(tmp_path / "recovery") as (hooks, _writer, _broker, _accounts):
        with pytest.raises(ProductionServicesUnavailable, match="RECOVERY"):
            hooks.startup()
        assert hooks.status.state is RuntimeState.HALTED
        assert "UNKNOWN_ORDER" in hooks.status.blockers

    RecoveryService.result = RecoveryResult()
    with hook_case(tmp_path / "reconcile") as (hooks, _writer, _broker, _accounts):

        def fail(_kind):
            raise RuntimeError("reconcile failed")

        monkeypatch.setattr(hooks, "_reconcile", fail)
        with pytest.raises(ProductionServicesUnavailable, match="STARTUP_RECONCILIATION"):
            hooks.startup()
        assert hooks.status.state is RuntimeState.HALTED


def test_canary_promotion_gate_is_identity_bound(tmp_path: Path) -> None:
    identities: dict[EvidenceStage, DeploymentIdentity] = {}

    def promotion_identity(stage: EvidenceStage) -> DeploymentIdentity:
        return identities[stage]

    with hook_case(
        tmp_path,
        mode=Mode.CANARY,
        promotion_identity_provider=promotion_identity,
    ) as (hooks, _writer, broker, _accounts):
        account_hash = broker.query_account().account_id_hash
        shadow_deployment = DeploymentIdentity(
            firmquant_commit=hooks._identity.firmquant_commit,
            uquant_commit=hooks._identity.uquant_commit,
            uquant_tree="2" * 40,
            uquant_package_manifest_sha256="3" * 64,
            uquant_code_fingerprint="4" * 64,
            uquant_config_fingerprint="5" * 64,
            semantic_config_sha256=hooks._identity.promotion_config_sha256,
            raw_config_sha256="6" * 64,
            xtquant_safety_manifest_sha256="7" * 64,
            account_id_hash=account_hash,
            account_authority_epoch=2,
            mode_epoch=3,
            mode=Mode.SHADOW,
            caps_sha256="8" * 64,
            production_policy_sha256="9" * 64,
        )
        identities[EvidenceStage.SHADOW] = shadow_deployment
        with pytest.raises(ProductionServicesUnavailable, match="PROMOTION"):
            hooks._require_promotion(account_hash)

        thresholds = hooks._settings.promotion
        store = ps.PromotionStore(hooks._database)
        orders_per_session = max(1, thresholds.min_shadow_orders)
        for index in range(thresholds.min_shadow_sessions):
            session = date.fromordinal(EXECUTION_SESSION.toordinal() - thresholds.min_shadow_sessions + index)
            decision_id = f"decision-shadow-{index}"
            operational_identity = OperationalEvidenceIdentity(
                deployment_identity=shadow_deployment,
                account_state_sha256="b" * 64,
                broker_snapshot_id=f"snapshot-shadow-{index}",
                broker_snapshot_sha256="0" * 64,
                broker_event_watermark=index,
                snapshot_started_at=NOW,
                snapshot_completed_at=NOW,
                snapshot_duration_ms=17,
                calendar_sha256="e" * 64,
                active_data_generation_sha256="d" * 64,
                strategy_data_manifest_sha256="d" * 64,
                strategy_session=session,
                decision_id=decision_id,
                phase="EXECUTION",
                kind="SHADOW_EXECUTION",
            )
            target = TargetObservation(
                symbol="600000.SH",
                target_shares=100,
                target_weight=Decimal("0.10"),
                reference_price=Decimal("10"),
            )
            position = PositionObservation(symbol="600000.SH", shares=100)
            orders = tuple(
                OrderObservation(
                    execution_id=f"shadow-{index}-{order_index}",
                    uquant_order_id=f"uq-shadow-{index}-{order_index}",
                    symbol="600000.SH",
                    side="BUY",
                    planned_shares=100,
                    filled_shares=0,
                    reference_price=Decimal("10"),
                    blocker=BlockerCode.TARGET_ALREADY_SATISFIED,
                )
                for order_index in range(orders_per_session)
            )
            store.append(
                ExecutionObservation(
                    identity=EvidenceIdentity(
                        stage=EvidenceStage.SHADOW,
                        execution_session=session,
                        firmquant_commit=hooks._identity.firmquant_commit,
                        uquant_commit=hooks._identity.uquant_commit,
                        promotion_config_sha256=hooks._identity.promotion_config_sha256,
                        account_sha256=account_hash,
                        data_sha256="d" * 64,
                        calendar_sha256="e" * 64,
                        operational_identity=operational_identity,
                    ),
                    decision_id=decision_id,
                    plan_id=f"plan-shadow-{index}",
                    portfolio_equity=Decimal("10000"),
                    planned_orders=orders,
                    planning_blockers=(),
                    targets=(target,),
                    fills=(),
                    actual_ending_positions=(position,),
                    hypothetical_ending_positions=(position,),
                    submit_count=0,
                    cancel_count=0,
                    rejection_count=0,
                    unknown_count=0,
                    external_activity=0,
                    duplicate_economic_orders=0,
                    duplicate_fills=0,
                    data_quality_failures=0,
                    created_at=NOW,
                )
            )
        hooks._require_promotion(account_hash)


def test_operational_callback_is_durable_and_halts_runtime(tmp_path: Path) -> None:
    with hook_case(tmp_path) as (hooks, _writer, _broker, _accounts):
        event = normalize_broker_event(
            {
                "event_id": "disconnect-1",
                "event_type": "DISCONNECTED",
                "payload": {
                    "session_date": EXECUTION_SESSION.isoformat(),
                    "event_time": NOW.isoformat(),
                },
            },
            received_at=NOW,
        )
        with pytest.raises(ProductionServicesUnavailable, match="BROKER_DISCONNECTED"):
            hooks.handle_event(event)
        assert hooks.status.state is RuntimeState.HALTED
        assert hooks._database.scalar("SELECT count(*) FROM broker_events") == 1
        assert hooks._database.scalar("SELECT count(*) FROM risk_events") == 1

        with pytest.raises(ProductionServicesUnavailable, match="EVENT_TYPE"):
            hooks.handle_event(object())


def test_audit_and_reconciliation_receipt_lookup_are_idempotent(tmp_path: Path) -> None:
    with hook_case(tmp_path) as (hooks, _writer, _broker, _accounts):
        event_id = "production-test-audit"
        hooks._audit(event_id, "RUNTIME", {"schema": "test.v1", "value": 1})
        hooks._audit(event_id, "RUNTIME", {"schema": "test.v1", "value": 1})
        assert hooks._audited(event_id)
        assert (
            hooks._database.scalar(
                "SELECT count(*) FROM audit_events WHERE audit_event_id = ?",
                (event_id,),
            )
            == 1
        )
        with pytest.raises(ProductionServicesUnavailable, match="RECEIPT_MISSING"):
            hooks._latest_passed_reconciliation(ReconciliationKind.EOD)


def test_post_close_decision_updates_data_and_atomically_persists_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with hook_case(tmp_path) as (hooks, _writer, _broker, accounts):
        decision = decision_snapshot()
        hooks._strategy = Strategy(decision)
        updater = DataUpdater()
        hooks._data_updater = updater
        hooks._decisions = SimpleNamespace(for_session=lambda _session: ())
        monkeypatch.setattr(hooks, "_capture", lambda: execution_snapshot().broker_snapshot)
        monkeypatch.setattr(
            hooks,
            "_latest_passed_reconciliation",
            lambda _kind: "recon_" + "c" * 64,
        )
        accounts.persist_result = decision.account_after_sha256

        assert hooks._post_close_decision(STRATEGY_SESSION) == 1
        assert updater.calls[0][1] == STRATEGY_SESSION
        assert accounts.persisted[0][1] == "DECISION_COMMIT"
        assert hooks._audited("production-decision:" + decision.decision_id)

        hooks._decisions = SimpleNamespace(for_session=lambda _session: (decision,))
        assert hooks._post_close_decision(STRATEGY_SESSION) == 0
        assert accounts.persisted[-1][1] == "DECISION_RECOVERY"
        assert hooks._audited("production-decision-recovery:" + decision.decision_id)

        accounts.store.hash_state = lambda _account: decision.account_after_sha256
        assert hooks._post_close_decision(STRATEGY_SESSION) == 0

        hooks._decisions = SimpleNamespace(for_session=lambda _session: ())
        accounts.persist_result = "0" * 64
        with pytest.raises(ProductionServicesUnavailable, match="COMMIT_MISMATCH"):
            hooks._post_close_decision(STRATEGY_SESSION)


def test_shadow_execution_records_hypothetical_orders_and_never_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = decision_snapshot(include_sell=False, include_buy=True)

    def identity_provider(
        stage: EvidenceStage,
        snapshot: BrokerSnapshot,
    ) -> OperationalEvidenceIdentity:
        assert stage is EvidenceStage.SHADOW
        deployment = DeploymentIdentity(
            firmquant_commit="f" * 40,
            uquant_commit="1" * 40,
            uquant_tree="2" * 40,
            uquant_package_manifest_sha256="3" * 64,
            uquant_code_fingerprint="4" * 64,
            uquant_config_fingerprint="5" * 64,
            semantic_config_sha256="c" * 64,
            raw_config_sha256="6" * 64,
            xtquant_safety_manifest_sha256="7" * 64,
            account_id_hash=snapshot.account.account_id_hash,
            account_authority_epoch=2,
            mode_epoch=3,
            mode=Mode.SHADOW,
            caps_sha256="8" * 64,
            production_policy_sha256="9" * 64,
        )
        return OperationalEvidenceIdentity(
            deployment_identity=deployment,
            account_state_sha256=decision.account_after_sha256,
            broker_snapshot_id=snapshot.snapshot_id,
            broker_snapshot_sha256=snapshot.raw_payload_sha256,
            broker_event_watermark=snapshot.broker_event_watermark,
            snapshot_started_at=NOW,
            snapshot_completed_at=NOW,
            snapshot_duration_ms=17,
            calendar_sha256=calendar().sha256,
            active_data_generation_sha256=decision.data_manifest_sha256,
            strategy_data_manifest_sha256=decision.data_manifest_sha256,
            strategy_session=decision.strategy_session,
            decision_id=decision.decision_id,
            phase="EXECUTION",
            kind="SHADOW_EXECUTION",
        )

    with hook_case(
        tmp_path,
        operational_identity_provider=identity_provider,
    ) as (hooks, _writer, broker, _accounts):
        facts = execution_snapshot()
        plan = ExecutionPlanner().plan(decision, facts)
        hooks._shadow_execute(plan, decision, facts)
        hooks._shadow_execute(plan, decision, facts)

        assert broker.submitted_commands == ()
        assert broker.cancelled_order_ids == ()
        assert hooks.real_order_calls() == 0
        deployment = identity_provider(
            EvidenceStage.SHADOW,
            facts.broker_snapshot,
        ).deployment_identity
        evidence = ps.PromotionStore(hooks._database).aggregate(
            stage=EvidenceStage.SHADOW,
            deployment_identity_sha256=deployment.sha256,
            account_authority_epoch=deployment.account_authority_epoch,
            mode_epoch=deployment.mode_epoch,
            mode=deployment.mode,
        )
        assert evidence is not None
        assert evidence.observed_sessions == 1
        assert evidence.order_count == len(plan.orders)

        complete_close(hooks)
        hooks._decisions = SimpleNamespace(for_session=lambda _session: (decision,))
        monkeypatch.setattr(
            hooks,
            "_reconcile",
            lambda _kind: (
                SimpleNamespace(reconciliation_id="recon_" + "d" * 64),
                facts.broker_snapshot,
                Account(),
            ),
        )
        monkeypatch.setattr(hooks, "_execution_facts", lambda _decision: facts)
        assert hooks._execute(EXECUTION_SESSION) == 1
        assert hooks._execute(EXECUTION_SESSION) == 0
        assert broker.submitted_commands == ()


def test_shadow_execution_without_canonical_identity_provider_fails_closed(tmp_path: Path) -> None:
    with hook_case(tmp_path) as (hooks, _writer, _broker, _accounts):
        decision = decision_snapshot(include_sell=False, include_buy=True)
        facts = execution_snapshot()
        plan = ExecutionPlanner().plan(decision, facts)
        with pytest.raises(
            ProductionServicesUnavailable,
            match="CANONICAL_OPERATIONAL_IDENTITY_UNAVAILABLE",
        ):
            hooks._shadow_execute(plan, decision, facts)


def test_execute_distinguishes_missing_decision_from_valid_frozen_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with hook_case(tmp_path) as (hooks, _writer, _broker, _accounts):
        decision = decision_snapshot()
        hooks._decisions = SimpleNamespace(for_session=lambda _session: (decision,))
        with pytest.raises(ProductionServicesUnavailable, match="MISSING_DECISION"):
            hooks._execute(EXECUTION_SESSION)
        assert "MISSING_DECISION" in hooks.status.blockers

    with hook_case(tmp_path / "completed") as (hooks, _writer, _broker, _accounts):
        complete_close(hooks)
        decision = decision_snapshot()
        hooks._decisions = SimpleNamespace(for_session=lambda _session: (decision, decision))
        with pytest.raises(ProductionServicesUnavailable, match="MULTIPLE"):
            hooks._execute(EXECUTION_SESSION)

        hooks._decisions = SimpleNamespace(for_session=lambda _session: (decision,))
        monkeypatch.setattr(
            hooks,
            "_reconcile",
            lambda _kind: (
                SimpleNamespace(reconciliation_id="recon_" + "d" * 64),
                execution_snapshot().broker_snapshot,
                Account(),
            ),
        )
        base = execution_snapshot()
        bad = replace(
            base,
            quotes=tuple(replace(quote, market_status=MarketSessionStatus.CLOSED) for quote in base.quotes),
            market_status=MarketSessionStatus.CLOSED,
        )
        monkeypatch.setattr(hooks, "_execution_facts", lambda _decision: bad)
        with pytest.raises(ProductionServicesUnavailable, match="MARKET_FACT"):
            hooks._execute(EXECUTION_SESSION)

        hooks._decisions = SimpleNamespace(for_session=lambda _session: ())
        with pytest.raises(ProductionServicesUnavailable, match="MISSING_DECISION"):
            hooks._execute(EXECUTION_SESSION)


def test_risk_helpers_track_external_activity_notionals_and_drawdown(
    tmp_path: Path,
) -> None:
    with hook_case(tmp_path) as (hooks, _writer, _broker, _accounts):
        assert hooks._known_client_ids() == frozenset()
        assert hooks._external_order_count() == 0
        assert hooks._system_cancel_allowed("missing") is False
        submitted, filled = hooks._notionals(EXECUTION_SESSION)
        assert submitted == Money(Decimal("0"))
        assert filled == Money(Decimal("0"))
        equity, intraday, drawdown = hooks._account_risk_fractions(execution_snapshot().broker_snapshot)
        assert equity == 0
        assert intraday == 0
        assert drawdown == 0


def test_cycle_uses_authoritative_calendar_and_fail_closed_state_machine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with hook_case(tmp_path / "open") as (hooks, _writer, _broker, _accounts):
        ready(hooks)
        monkeypatch.setattr(hooks, "_execute", lambda _session: 1)
        result = hooks.cycle(NOW)
        assert result.executions == 1
        assert hooks.status.state is RuntimeState.READY

    with hook_case(
        tmp_path / "closed",
        status=MarketSessionStatus.CLOSED,
        clock=lambda: POST_CLOSE,
    ) as (hooks, _writer, _broker, _accounts):
        ready(hooks)
        monkeypatch.setattr(hooks, "_close_session", lambda _session: (1, 1))
        result = hooks.cycle(POST_CLOSE)
        assert result.eod == 1
        assert result.decisions == 1
        assert hooks.status.state is RuntimeState.READY

    with hook_case(tmp_path / "holiday") as (hooks, _writer, _broker, _accounts):
        ready(hooks)
        result = hooks.cycle(datetime(2026, 8, 22, 2, tzinfo=UTC))
        assert result == ps.ProductionCycleResult(0, 0, 0)

    with hook_case(tmp_path / "expired") as (hooks, _writer, _broker, _accounts):
        ready(hooks)
        with pytest.raises(ProductionServicesUnavailable, match="CALENDAR_COVERAGE"):
            hooks.cycle(datetime(2026, 9, 1, 2, tzinfo=UTC))
        assert hooks.status.state is RuntimeState.HALTED


def test_close_session_orders_decision_before_report_and_backup_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with hook_case(tmp_path) as (hooks, _writer, _broker, _accounts):
        events: list[str] = []
        decision = decision_snapshot()
        snapshot = execution_snapshot().broker_snapshot
        hooks._decisions = SimpleNamespace(for_session=lambda _session: (decision,))
        hooks._data_generations = SimpleNamespace(
            active=lambda: SimpleNamespace(path=hooks._settings.paths.data_directory)
        )
        hooks._data_root = hooks._settings.paths.data_directory
        deployment = DeploymentIdentity(
            firmquant_commit="f" * 40,
            uquant_commit="1" * 40,
            uquant_tree="2" * 40,
            uquant_package_manifest_sha256="3" * 64,
            uquant_code_fingerprint="4" * 64,
            uquant_config_fingerprint="5" * 64,
            semantic_config_sha256="6" * 64,
            raw_config_sha256=hooks._identity.config_sha256,
            xtquant_safety_manifest_sha256="8" * 64,
            account_id_hash=snapshot.account.account_id_hash,
            account_authority_epoch=1,
            mode_epoch=1,
            mode=Mode.SHADOW,
            caps_sha256="9" * 64,
            production_policy_sha256="a" * 64,
        )
        operational = OperationalEvidenceIdentity(
            deployment_identity=deployment,
            account_state_sha256=decision.account_after_sha256,
            broker_snapshot_id=snapshot.snapshot_id,
            broker_snapshot_sha256=snapshot.raw_payload_sha256,
            broker_event_watermark=snapshot.broker_event_watermark,
            snapshot_started_at=NOW,
            snapshot_completed_at=NOW,
            snapshot_duration_ms=1,
            calendar_sha256="b" * 64,
            active_data_generation_sha256="c" * 64,
            strategy_data_manifest_sha256="d" * 64,
            strategy_session=EXECUTION_SESSION,
            decision_id=decision.decision_id,
            phase="EOD",
            kind="BACKUP",
        )
        wrong_mode = replace(
            operational,
            deployment_identity=replace(deployment, mode=Mode.CANARY),
        )
        supplied_identities = [
            replace(operational, phase="STARTUP", kind="SMOKE"),
            replace(operational, broker_snapshot_id="snapshot-other"),
            wrong_mode,
            operational,
        ]
        hooks._operational_identity_provider = lambda _stage, _snapshot: supplied_identities.pop(0)
        with hooks._database.transaction():
            hooks._database.write(
                """
                INSERT INTO account_authority_epochs(
                    epoch,account_id_hash,account_state_sha256,deployment_identity_sha256,
                    source_binding_id,payload_json,payload_sha256,created_at
                ) VALUES(1,?,?,NULL,NULL,'{}',?,?)
                """,
                (
                    snapshot.account.account_id_hash,
                    decision.account_after_sha256,
                    "e" * 64,
                    NOW.isoformat(),
                ),
            )
            hooks._database.write("INSERT INTO account_authority_active(singleton_id,epoch) VALUES(1,1)")
            hooks._database.write(
                """
                INSERT INTO mode_epochs(
                    epoch,mode,deployment_identity_sha256,caps_sha256,
                    payload_json,payload_sha256,created_at
                ) VALUES(1,'SHADOW',NULL,NULL,'{}',?,?)
                """,
                ("f" * 64, NOW.isoformat()),
            )
            hooks._database.write("INSERT INTO mode_epoch_active(singleton_id,epoch) VALUES(1,1)")
        hooks._snapshots.persist(snapshot, started_at=NOW, completed_at=NOW, duration_ms=1)
        monkeypatch.setattr(
            hooks,
            "_reconcile",
            lambda _kind: (
                events.append("reconcile") or SimpleNamespace(reconciliation_id="recon_" + "e" * 64),
                snapshot,
                Account(),
            ),
        )
        monkeypatch.setattr(hooks, "_capture", lambda: snapshot)
        monkeypatch.setattr(
            hooks._data_updater,
            "update",
            lambda _symbols, *, through: (
                events.append("data")
                or SimpleNamespace(
                    manifest_sha256="d" * 64,
                    governance_manifest_sha256="g" * 64,
                    data_generation_id="gen-test",
                    fetch_attempts=1,
                )
            ),
        )
        hooks._data_reloader = lambda _root: events.append("reload")
        monkeypatch.setattr(
            hooks,
            "_post_close_decision",
            lambda *_args, **_kwargs: events.append("decision") or 1,
        )

        class Builder:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def build(self, _session):
                events.append("report")
                return SimpleNamespace(decision_id=decision.decision_id)

        class Renderer:
            def write(self, _report, _directory):
                return SimpleNamespace(
                    report_id="report-1",
                    json_sha256="1" * 64,
                    markdown_sha256="2" * 64,
                )

        monkeypatch.setattr(ps, "DatabaseDailyReportBuilder", Builder)
        monkeypatch.setattr(ps, "DailyReportRenderer", Renderer)
        monkeypatch.setattr(
            ps,
            "backup_state",
            lambda *_args, **_kwargs: (
                events.append("backup") or SimpleNamespace(backup_id="backup-1", manifest_sha256="a" * 64)
            ),
        )
        monkeypatch.setattr(hooks._accounts.store, "hash_file", lambda _path: "e" * 64)

        with pytest.raises(ProductionServicesUnavailable, match="BACKUP_OPERATIONAL_IDENTITY"):
            hooks._close_session(EXECUTION_SESSION)
        assert events == ["reconcile", "data", "reload", "decision", "report"]
        assert hooks._close.load(EXECUTION_SESSION, CloseStep.BACKUP_VERIFIED) is None
        with pytest.raises(ProductionServicesUnavailable, match="BACKUP_OPERATIONAL_IDENTITY"):
            hooks._close_session(EXECUTION_SESSION)
        with pytest.raises(ProductionServicesUnavailable, match="BACKUP_OPERATIONAL_IDENTITY"):
            hooks._close_session(EXECUTION_SESSION)

        assert hooks._close_session(EXECUTION_SESSION) == (0, 1)
        assert events == ["reconcile", "data", "reload", "decision", "report", "backup"]
        assert hooks._close.completed(EXECUTION_SESSION) is not None
        assert hooks._close.load(EXECUTION_SESSION, CloseStep.REPORT_PUBLISHED) is not None
        assert hooks._close.load(EXECUTION_SESSION, CloseStep.BACKUP_VERIFIED) is not None
        before = list(events)
        assert hooks._close_session(EXECUTION_SESSION) == (0, 0)
        assert events == before


def test_backup_evidence_stage_never_relabels_live_as_canary() -> None:
    assert ps._backup_evidence_stage(Mode.SHADOW) is EvidenceStage.SHADOW
    assert ps._backup_evidence_stage(Mode.CANARY) is EvidenceStage.CANARY
    with pytest.raises(ProductionServicesUnavailable, match=r"BACKUP.*LIVE"):
        ps._backup_evidence_stage(Mode.LIVE)


def test_builder_is_fail_closed_and_composes_single_daemon_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, config = settings_for(tmp_path, Mode.SHADOW)
    source = settings.paths.uquant_source_checkout
    assert source is not None
    safety = safety_manifest()
    calendar_value = calendar()
    broker = broker_for()

    with WriterLease.acquire(
        tmp_path / "state" / "firmquant.sqlite3",
        owner="builder-test",
        clock=lambda: NOW,
    ) as writer:
        with pytest.raises(ProductionServicesUnavailable, match="SHADOW"):
            ps.build_production_runtime(
                config_path=config,
                settings=Settings(),
                writer=writer,
                clock=lambda: NOW,
            )

        monkeypatch.setattr(ps.XtQuantSafetyManifest, "load", lambda _path: safety)
        monkeypatch.setattr(
            ps.StrategyIdentity,
            "locked",
            lambda: SimpleNamespace(
                uquant_commit="1" * 40,
                canonical_universe_sha256="0" * 64,
                config_fingerprint="0" * 64,
                economic_code_fingerprint="0" * 64,
                verify=lambda: None,
            ),
        )
        monkeypatch.setattr(ps, "current_clean_firmquant_commit", lambda: "f" * 40)
        monkeypatch.setattr(
            ps,
            "configuration_sha256",
            lambda _path: hashlib.sha256(config.read_bytes()).hexdigest(),
        )
        monkeypatch.setattr(ps, "promotion_config_sha256", lambda _settings: "p" * 64)
        monkeypatch.setattr(ps, "load_trading_calendar_manifest", lambda _path: calendar_value)
        monkeypatch.setattr(ps, "build_production_xtquant_gateway", lambda **_kwargs: broker)
        monkeypatch.setattr(ps, "_load_engine", lambda *_args: SimpleNamespace())
        monkeypatch.setattr(ps.UniversePolicy, "from_uquant", lambda *_args, **_kwargs: Universe())
        monkeypatch.setattr(ps, "OfficialXtQuantDailyHistoryProvider", lambda **_kwargs: object())
        monkeypatch.setattr(ps, "XtQuantDailyDataUpdater", lambda **_kwargs: DataUpdater())
        monkeypatch.setattr(ps, "RuntimeAccountRepository", lambda **_kwargs: Accounts(tmp_path))
        monkeypatch.setattr(ps, "StrategyAdapter", lambda **_kwargs: Strategy(decision_snapshot()))
        real_import = ps.importlib.import_module
        monkeypatch.setattr(
            ps.importlib,
            "import_module",
            lambda name: SimpleNamespace() if name == "xtquant.xtdata" else real_import(name),
        )
        monkeypatch.setattr(ps, "_install_stop_handlers", lambda _flag: None)

        runtime = ps.build_production_runtime(
            config_path=config,
            settings=settings,
            writer=writer,
            clock=lambda: NOW,
        )
        assert isinstance(runtime, ps.ProductionDaemon)
        assert runtime._mode is Mode.SHADOW
        assert isinstance(runtime._hooks, ProductionServiceHooks)


def test_stop_flag_and_signal_handler_are_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flag = ps._StopFlag()
    assert flag() is False
    flag.request(0, None)
    assert flag() is True

    registrations: list[object] = []
    monkeypatch.setattr(ps.signal, "signal", lambda sig, handler: registrations.append((sig, handler)))
    ps._install_stop_handlers(ps._StopFlag())
    assert registrations
