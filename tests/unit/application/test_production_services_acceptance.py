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
from firmquant.application.production_events import ProductionEventJournal
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
from firmquant.domain.broker_facts import MarketSessionStatus
from firmquant.domain.states import RuntimeState, RuntimeStatus
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


class Universe:
    deployment_symbols = ("sz300308", "sz300502")

    def allowed(self, symbol: str, _as_of: date) -> bool:
        return symbol in self.deployment_symbols


class PassingReconciler:
    def __init__(self) -> None:
        self.facts: list[object] = []

    def run(self, kind, facts):
        self.facts.append(facts)
        return SimpleNamespace(
            reconciliation_id="recon_" + "a" * 64,
            kind=kind,
            passed=True,
            blockers=(),
        )


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
):
    settings, config = settings_for(root, mode)
    identity = ps._RuntimeIdentity(
        firmquant_commit="f" * 40,
        uquant_commit="1" * 40,
        config_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
        promotion_config_sha256="p" * 64,
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
        )
        try:
            yield hooks, writer, broker, account_repository
        finally:
            broker.disconnect()


def ready(hooks: ProductionServiceHooks) -> None:
    hooks._status = RuntimeStatus(
        state=RuntimeState.READY,
        revision=3,
        reason="ready for test",
        blockers=(),
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

    assert ps._fee_schedule(safety_manifest()).minimum_commission == Money(Decimal("5"))
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
    with hook_case(tmp_path) as (hooks, _writer, _broker, accounts):
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
            run=lambda _kind, _facts: SimpleNamespace(
                passed=False,
                blockers=("BROKER_MISMATCH",),
            )
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
    with hook_case(tmp_path, mode=Mode.CANARY) as (hooks, _writer, broker, _accounts):
        account_hash = broker.query_account().account_id_hash
        with pytest.raises(ProductionServicesUnavailable, match="PROMOTION"):
            hooks._require_promotion(account_hash)

        thresholds = hooks._settings.promotion
        ps.PromotionStore(hooks._database).append(
            ps.ShadowPromotionEvidence(
                firmquant_commit=hooks._identity.firmquant_commit,
                uquant_commit=hooks._identity.uquant_commit,
                config_sha256=hooks._identity.promotion_config_sha256,
                account_hash=account_hash,
                observed_sessions=thresholds.min_shadow_sessions,
                hypothetical_orders=thresholds.min_shadow_orders,
                unresolved_orders=0,
                external_orders=0,
                duplicate_economic_orders=0,
                duplicate_fills=0,
                max_target_tracking_error=Decimal("0"),
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

        hooks._decisions = SimpleNamespace(for_session=lambda _session: ())
        accounts.persist_result = "0" * 64
        with pytest.raises(ProductionServicesUnavailable, match="COMMIT_MISMATCH"):
            hooks._post_close_decision(STRATEGY_SESSION)


def test_shadow_execution_records_hypothetical_orders_and_never_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with hook_case(tmp_path) as (hooks, _writer, broker, _accounts):
        decision = decision_snapshot(include_sell=False, include_buy=True)
        facts = execution_snapshot()
        plan = ExecutionPlanner().plan(decision, facts)
        hooks._shadow_execute(plan, decision)
        hooks._shadow_execute(plan, decision)

        assert broker.submitted_commands == ()
        assert broker.cancelled_order_ids == ()
        assert hooks.real_order_calls() == 0
        evidence = ps.PromotionStore(hooks._database).latest(
            firmquant_commit=hooks._identity.firmquant_commit,
            uquant_commit=hooks._identity.uquant_commit,
            config_sha256=hooks._identity.promotion_config_sha256,
            account_hash=broker.query_account().account_id_hash,
        )
        assert evidence is not None
        assert evidence.observed_sessions == 2
        assert evidence.hypothetical_orders == len(plan.orders) * 2

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


def test_execute_fails_closed_for_ambiguous_or_invalid_market_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with hook_case(tmp_path) as (hooks, _writer, _broker, _accounts):
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
        bad = replace(
            execution_snapshot(),
            market_status=MarketSessionStatus.CLOSED,
        )
        monkeypatch.setattr(hooks, "_execution_facts", lambda _decision: bad)
        with pytest.raises(ProductionServicesUnavailable, match="MARKET_FACT"):
            hooks._execute(EXECUTION_SESSION)

        hooks._decisions = SimpleNamespace(for_session=lambda _session: ())
        assert hooks._execute(EXECUTION_SESSION) == 0


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
        monkeypatch.setattr(hooks, "_eod", lambda _session: 1)
        monkeypatch.setattr(hooks, "_post_close_decision", lambda _session: 1)
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


def test_eod_runs_reconciliation_report_backup_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with hook_case(tmp_path) as (hooks, _writer, _broker, _accounts):
        monkeypatch.setattr(
            hooks,
            "_reconcile",
            lambda _kind: (
                SimpleNamespace(reconciliation_id="recon_" + "e" * 64),
                execution_snapshot().broker_snapshot,
                Account(),
            ),
        )

        class Builder:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def build(self, _session):
                return SimpleNamespace(report_id="report-1")

        class Renderer:
            def write(self, _report, _directory):
                return SimpleNamespace(report_id="report-1")

        monkeypatch.setattr(ps, "DatabaseDailyReportBuilder", Builder)
        monkeypatch.setattr(ps, "DailyReportRenderer", Renderer)
        monkeypatch.setattr(
            ps,
            "backup_state",
            lambda *_args, **_kwargs: SimpleNamespace(
                backup_id="backup-1",
                manifest_sha256="a" * 64,
            ),
        )
        assert hooks._eod(EXECUTION_SESSION) == 1
        assert hooks._eod(EXECUTION_SESSION) == 0
        assert hooks._audited("production-eod:" + EXECUTION_SESSION.isoformat())


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
