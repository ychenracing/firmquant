from __future__ import annotations

import hashlib
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import firmquant.application.production_services as ps
import tests.unit.application.test_production_services_acceptance as base
from firmquant.broker.fake import BrokerOperation, ScriptedOutcome
from firmquant.broker.gateway import BrokerOrderCommand
from firmquant.broker.normalization import normalize_order
from firmquant.config import Mode, Settings
from firmquant.domain.broker_facts import BrokerOrderStatus, MarketSessionStatus, PriceType
from firmquant.domain.states import RuntimeState, RuntimeStatus
from firmquant.domain.values import Money, Shares
from firmquant.execution.planner import ExecutionPlanner
from firmquant.reconciliation.models import ReconciliationKind
from firmquant.risk.arm import ArmBinding, ArmService
from firmquant.risk.gate import GateAction, GateDecision
from firmquant.security.secrets import SecretBytes
from tests.fixtures.session_cases import NOW, STRATEGY_SESSION, decision_snapshot, execution_snapshot


def _command(planned) -> BrokerOrderCommand:
    return BrokerOrderCommand(
        execution_id="exec_" + "a" * 64,
        idempotency_key="b" * 64,
        client_order_id=planned.uquant_order_id,
        symbol=planned.symbol,
        side=planned.side,
        price_type=PriceType.LIMIT,
        requested_shares=planned.uquant_authorized_shares,
        limit_price=planned.limit_price,
        strategy_session=planned.strategy_session,
    )


def _insert_arm(
    hooks: ps.ProductionServiceHooks,
    *,
    ttl: timedelta = timedelta(minutes=5),
    mac_key: bytes = b"k" * 32,
    corrupt_mac: bool = False,
) -> None:
    account_hash = hooks._broker.query_account().account_id_hash
    binding = ArmBinding(
        mode=hooks._settings.mode,
        host_hash=hooks._writer.host_hash,
        account_hash=account_hash,
        firmquant_commit=hooks._identity.firmquant_commit,
        uquant_commit=hooks._identity.uquant_commit,
        config_sha256=hooks._identity.config_sha256,
    )
    service = ArmService(
        mac_key=SecretBytes(mac_key),
        lease_id_factory=lambda: "arm_" + "a" * 32,
    )
    lease = service.issue(
        binding,
        now=NOW,
        confirmation_reader=lambda: service.confirmation_phrase(hooks._settings.mode),
        interactive_terminal=True,
        environment={},
        ttl=ttl,
    )
    with hooks._database.transaction():
        hooks._database.write(
            """
            INSERT INTO arm_leases(
                lease_id,mode,host_hash,account_hash,firmquant_commit,uquant_commit,
                config_sha256,identity_payload_sha256,issued_at,expires_at,revoked_at,
                revoke_reason,lease_mac
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                lease.lease_id,
                lease.mode.value,
                lease.host_hash,
                lease.account_hash,
                lease.firmquant_commit,
                lease.uquant_commit,
                lease.config_sha256,
                lease.identity_payload_sha256,
                lease.issued_at.isoformat(),
                lease.expires_at.isoformat(),
                None,
                None,
                "0" * 64 if corrupt_mac else lease.lease_mac,
            ),
        )


def test_strategy_view_rejects_malformed_account_shapes_and_handles_unmarked_positions(
    tmp_path: Path,
) -> None:
    repository = base.Accounts(tmp_path)
    account = base.Account()

    account.payload["positions"] = []
    with pytest.raises(ps.ProductionServicesUnavailable, match="STATE_INVALID"):
        ps._strategy_view(account, (), repository)

    account = base.Account()
    account.payload["order_ledger"] = {}
    with pytest.raises(ps.ProductionServicesUnavailable, match="STATE_INVALID"):
        ps._strategy_view(account, (), repository)

    account = base.Account()
    account.payload["positions"] = {1: {"shares": 1}}
    with pytest.raises(ps.ProductionServicesUnavailable, match="POSITION_INVALID"):
        ps._strategy_view(account, (), repository)

    account = base.Account()
    account.payload["positions"] = {"sz300308": []}
    with pytest.raises(ps.ProductionServicesUnavailable, match="POSITION_INVALID"):
        ps._strategy_view(account, (), repository)

    account = base.Account()
    account.payload["positions"] = {"sz300308": {"shares": 0}}
    with pytest.raises(ps.ProductionServicesUnavailable, match="POSITION_INVALID"):
        ps._strategy_view(account, (), repository)

    account = base.Account()
    account.payload["order_ledger"] = [{}]
    with pytest.raises(ps.ProductionServicesUnavailable, match="ORDER_LEDGER_INVALID"):
        ps._strategy_view(account, (), repository)

    account = base.Account()
    view = ps._strategy_view(account, (), repository)
    assert view.total_assets == Money(Decimal("1000"))
    assert view.positions[0].sellable_shares == Shares(0)

    broker_position = execution_snapshot().broker_snapshot.positions[0]
    mismatched = replace(
        broker_position,
        total_shares=Shares(900),
        sellable_shares=Shares(800),
    )
    view = ps._strategy_view(base.Account(), (mismatched,), repository)
    assert view.total_assets == Money(Decimal("1000"))
    assert view.positions[0].sellable_shares == Shares(800)


def test_data_identity_and_uquant_execution_config_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = base.Account()
    account.payload["data_hash"] = ""
    assert ps._data_identity_matches(account, tmp_path) is False

    account = base.Account()
    account.payload["data_hash_symbols"] = []
    assert ps._data_identity_matches(account, tmp_path) is False

    monkeypatch.setattr(ps.importlib, "import_module", lambda _name: SimpleNamespace())
    assert ps._data_identity_matches(base.Account(), tmp_path) is False

    class BrokenStore:
        def __init__(self, _root: Path) -> None:
            raise RuntimeError("broken")

    monkeypatch.setattr(
        ps.importlib,
        "import_module",
        lambda _name: SimpleNamespace(DataStore=BrokenStore),
    )
    assert ps._data_identity_matches(base.Account(), tmp_path) is False

    class Manifest:
        digest = "d" * 64
        end = "2026-08-24"
        symbols = ("sz300308",)

    class Store:
        def __init__(self, _root: Path) -> None:
            pass

        def manifest(self, symbols, *, as_of):
            assert tuple(symbols) == ("sz300308",)
            assert as_of == "2026-08-24"
            return Manifest()

    monkeypatch.setattr(
        ps.importlib,
        "import_module",
        lambda _name: SimpleNamespace(DataStore=Store),
    )
    assert ps._data_identity_matches(base.Account(), tmp_path) is True
    Manifest.digest = "e" * 64
    assert ps._data_identity_matches(base.Account(), tmp_path) is False

    monkeypatch.setattr(
        ps.importlib,
        "import_module",
        lambda _name: SimpleNamespace(DEFAULT_CONFIG=SimpleNamespace(max_volume_participation=0.025)),
    )
    assert ps._uquant_participation() == Decimal("0.025")
    monkeypatch.setattr(ps.importlib, "import_module", lambda _name: SimpleNamespace(DEFAULT_CONFIG=None))
    with pytest.raises(ps.ProductionServicesUnavailable, match="CONFIG_UNAVAILABLE"):
        ps._uquant_participation()


def test_decision_symbol_and_engine_contract_failures_are_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ps.ProductionServicesUnavailable, match="ORDER_PAYLOAD"):
        ps._decision_symbols(SimpleNamespace(uquant_payload={"orders": [1]}))

    source = tmp_path / "source"
    engine = source / "uquant" / "engine.py"
    engine.parent.mkdir(parents=True)
    engine.write_text("DEFAULT_CONFIG = object()\n", encoding="utf-8")
    sys.modules.pop("firmquant_verified_uquant_engine", None)
    with pytest.raises(ps.ProductionServicesUnavailable, match="CONTRACT_UNAVAILABLE"):
        ps._load_engine(source, tmp_path / "data")
    sys.modules.pop("firmquant_verified_uquant_engine", None)

    engine.write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="boom"):
        ps._load_engine(source, tmp_path / "data")
    assert "firmquant_verified_uquant_engine" not in sys.modules

    monkeypatch.setattr(ps.importlib.util, "spec_from_file_location", lambda *_args: None)
    with pytest.raises(ps.ProductionServicesUnavailable, match="LOAD_FAILED"):
        ps._load_engine(source, tmp_path / "data")


def test_clock_runtime_state_heartbeat_and_halt_control_fail_closed(tmp_path: Path) -> None:
    with base.hook_case(tmp_path) as (hooks, _writer, _broker, _accounts):
        hooks._clock = lambda: datetime(2026, 8, 25, 9, 30)
        with pytest.raises(ps.ProductionServicesUnavailable, match="CLOCK_INVALID"):
            hooks._now()

    with base.hook_case(tmp_path / "state") as (hooks, _writer, _broker, _accounts):
        with pytest.raises(TypeError, match="heartbeat"):
            hooks.heartbeat(object())
        hooks.heartbeat(ps.ProductionHeartbeat(sequence=1, observed_at=NOW))
        hooks.halt("")
        assert hooks.status.state is RuntimeState.HALTED
        revision = hooks.status.revision
        hooks.halt("OTHER")
        assert hooks.status.revision == revision
        hooks._status = RuntimeStatus(
            state=RuntimeState.HALTED,
            revision=0,
            reason="halted",
            blockers=("HALTED",),
        )
        with pytest.raises(ps.ProductionServicesUnavailable, match="RESUME"):
            hooks.startup()
        hooks._status = RuntimeStatus(
            state=RuntimeState.STOPPING,
            revision=0,
            reason="stopping",
            blockers=(),
        )
        with pytest.raises(ps.ProductionServicesUnavailable, match="RESUME"):
            hooks.startup()


def test_arm_loading_covers_missing_invalid_expiring_and_valid_leases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIRMQUANT_SECRET_ARM_MAC_KEY", "k" * 32)
    with base.hook_case(tmp_path / "missing", mode=Mode.CANARY) as (hooks, _writer, broker, _accounts):
        with pytest.raises(ps.ProductionServicesUnavailable, match="LEASE_REQUIRED"):
            hooks._load_arm(broker.query_account().account_id_hash)

    with base.hook_case(tmp_path / "invalid", mode=Mode.CANARY) as (hooks, _writer, broker, _accounts):
        _insert_arm(hooks, corrupt_mac=True)
        with pytest.raises(ps.ProductionServicesUnavailable, match="LEASE_INVALID"):
            hooks._load_arm(broker.query_account().account_id_hash)

    with base.hook_case(tmp_path / "expiring", mode=Mode.CANARY) as (hooks, _writer, broker, _accounts):
        _insert_arm(hooks, ttl=timedelta(seconds=1))
        with pytest.raises(ps.ProductionServicesUnavailable, match="TOO_CLOSE"):
            hooks._load_arm(broker.query_account().account_id_hash)

    with base.hook_case(tmp_path / "valid", mode=Mode.CANARY) as (hooks, _writer, broker, _accounts):
        _insert_arm(hooks)
        service, lease, binding = hooks._load_arm(broker.query_account().account_id_hash)
        service.verify(lease, binding=binding, now=NOW)


def test_order_ownership_notional_and_drawdown_helpers_cover_nonzero_paths(tmp_path: Path) -> None:
    with base.hook_case(tmp_path) as (hooks, _writer, broker, accounts):
        with hooks._database.transaction():
            hooks._database.write(
                """
                INSERT INTO decision_snapshots(
                    decision_id,strategy_session,input_fingerprint,firmquant_commit,uquant_commit,
                    uquant_code_fingerprint,uquant_config_fingerprint,data_manifest_sha256,
                    universe_manifest_sha256,broker_snapshot_sha256,account_before_sha256,
                    account_after_sha256,payload_json,payload_sha256,created_at,supersedes_decision_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "decision_" + "1" * 64,
                    STRATEGY_SESSION.isoformat(),
                    "2" * 64,
                    "f" * 40,
                    "1" * 40,
                    "3" * 64,
                    "4" * 64,
                    "5" * 64,
                    "6" * 64,
                    "7" * 64,
                    "8" * 64,
                    "9" * 64,
                    "{}",
                    hashlib.sha256(b"{}").hexdigest(),
                    NOW.isoformat(),
                    None,
                ),
            )
            hooks._database.write(
                """
                INSERT INTO execution_intents(
                    execution_id,decision_id,idempotency_key,uquant_order_id,symbol,side,
                    requested_shares,filled_shares,state,strategy_session,uquant_source_sha,
                    aggregate_json,version,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "exec_" + "a" * 64,
                    "decision_" + "1" * 64,
                    "b" * 64,
                    "O-1",
                    "sz300308",
                    "SELL",
                    100,
                    50,
                    "ACKNOWLEDGED",
                    STRATEGY_SESSION.isoformat(),
                    "1" * 40,
                    "{}",
                    1,
                    NOW.isoformat(),
                    NOW.isoformat(),
                ),
            )
            hooks._database.write(
                """
                INSERT INTO broker_orders(
                    broker_order_id,execution_id,ownership,client_order_id,symbol,side,status,
                    requested_shares,filled_shares,limit_price,session_date,last_event_sequence,
                    event_time,received_at,raw_payload_sha256
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "broker-1",
                    "exec_" + "a" * 64,
                    "SYSTEM",
                    "O-1",
                    "sz300308",
                    "SELL",
                    "ACKNOWLEDGED",
                    100,
                    50,
                    "10.00",
                    base.EXECUTION_SESSION.isoformat(),
                    10,
                    NOW.isoformat(),
                    NOW.isoformat(),
                    "c" * 64,
                ),
            )
        assert hooks._known_client_ids() == frozenset({"O-1"})
        assert hooks._system_cancel_allowed("broker-1") is True
        submitted, filled = hooks._notionals(base.EXECUTION_SESSION)
        assert submitted == Money(Decimal("1000"))
        assert filled == Money(Decimal("500"))

        with hooks._database.transaction():
            hooks._database.write(
                "UPDATE broker_orders SET limit_price = NULL WHERE broker_order_id = ?",
                ("broker-1",),
            )
        with pytest.raises(ps.ProductionServicesUnavailable, match="PRICE_MISSING"):
            hooks._notionals(base.EXECUTION_SESSION)

        accounts.account.payload["capital_peak"] = 20000.0
        earlier = replace(
            execution_snapshot().broker_snapshot,
            snapshot_id="earlier",
            captured_at=NOW - timedelta(minutes=5),
            account=replace(
                execution_snapshot().broker_snapshot.account,
                total_assets=Money(Decimal("15000")),
            ),
        )
        hooks._snapshots.persist(earlier)
        current = replace(
            execution_snapshot().broker_snapshot,
            snapshot_id="current",
            account=replace(
                execution_snapshot().broker_snapshot.account,
                total_assets=Money(Decimal("10000")),
            ),
        )
        equity, intraday, drawdown = hooks._account_risk_fractions(current)
        assert equity > 0
        assert intraday > 0
        assert drawdown == Decimal("0.5")
        broker.set_orders((replace(execution_snapshot().broker_snapshot.orders[0], client_order_id=None),)) if execution_snapshot().broker_snapshot.orders else None


def test_risk_context_and_capability_submit_follow_single_authorized_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIRMQUANT_SECRET_ARM_MAC_KEY", "k" * 32)
    with base.hook_case(tmp_path, mode=Mode.CANARY) as (hooks, _writer, broker, _accounts):
        base.ready(hooks)
        _insert_arm(hooks)
        hooks._startup_reconciliation_id = "recon_" + "1" * 64
        monkeypatch.setattr(hooks, "_latest_passed_reconciliation", lambda _kind: "recon_" + "2" * 64)
        monkeypatch.setattr(ps, "_data_identity_matches", lambda *_args: True)
        monkeypatch.setattr(ps, "_uquant_participation", lambda: Decimal("0.05"))
        monkeypatch.setattr(ps, "configuration_sha256", lambda _path: hooks._identity.config_sha256)
        monkeypatch.setattr(
            ps.StrategyIdentity,
            "locked",
            lambda: SimpleNamespace(uquant_commit=hooks._identity.uquant_commit),
        )
        monkeypatch.setattr(
            ps.ExecutionRiskGate,
            "evaluate",
            lambda _self, command, _context: GateDecision(
                action=GateAction.ALLOW,
                authorized_shares=command.uquant_authorized_shares,
                reason_codes=("ALL_CHECKS_PASSED",),
            ),
        )
        decision = decision_snapshot(include_sell=False, include_buy=True)
        facts = execution_snapshot()
        plan = ExecutionPlanner().plan(decision, facts)
        planned = plan.orders[0]
        authorities = ps._ExecutionAuthorities(
            plan=plan,
            facts=facts,
            decision=decision,
            planned={planned.uquant_order_id: planned},
        )
        command = _command(planned)
        limits = ps.risk_limits_from_settings(hooks._settings)
        context = hooks._risk_context(command, planned, authorities, facts.broker_snapshot, limits)
        assert context.runtime_state is RuntimeState.READY
        assert context.freeze_new_risk is False
        assert context.broker_connected is True

        accepted = normalize_order(
            {
                "broker_order_id": "cap-order-1",
                "client_order_id": command.client_order_id,
                "symbol": command.symbol.canonical,
                "side": command.side.value,
                "price_type": "LIMIT",
                "status": BrokerOrderStatus.ACKNOWLEDGED.value,
                "requested_shares": command.requested_shares.value,
                "filled_shares": 0,
                "limit_price": command.limit_price.canonical,
                "session_date": base.EXECUTION_SESSION.isoformat(),
                "event_time": NOW.isoformat(),
                "event_sequence": 10,
            },
            received_at=NOW,
        )
        broker.script((ScriptedOutcome(BrokerOperation.SUBMIT, response=accepted),))
        capability = hooks._capability(authorities)
        response = capability.submit_order(command)
        assert response.broker_order_id == "cap-order-1"
        assert len(broker.submitted_commands) == 1

        bad = replace(command, client_order_id="UNKNOWN")
        with pytest.raises(Exception):
            capability.submit_order(bad)


def test_live_execute_path_audits_result_and_rejects_safety_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = decision_snapshot(include_sell=False, include_buy=True)
    facts = execution_snapshot()

    class Result:
        plan_id = "plan-1"
        outcomes = ()
        submit_calls = 1
        cancel_calls = 1
        unresolved_unknown = False
        negative_cash = False

    class Controller:
        def __init__(self, **_kwargs) -> None:
            pass

        def execute(self, _plan):
            return Result()

    with base.hook_case(tmp_path / "ok", mode=Mode.CANARY) as (hooks, _writer, _broker, _accounts):
        hooks._decisions = SimpleNamespace(for_session=lambda _session: (decision,))
        monkeypatch.setattr(
            hooks,
            "_reconcile",
            lambda _kind: (
                SimpleNamespace(reconciliation_id="recon_" + "3" * 64),
                facts.broker_snapshot,
                base.Account(),
            ),
        )
        monkeypatch.setattr(hooks, "_execution_facts", lambda _decision: facts)
        monkeypatch.setattr(hooks, "_require_promotion", lambda _account_hash: None)
        monkeypatch.setattr(hooks, "_capability", lambda _authorities: object())
        monkeypatch.setattr(ps, "LiveExecutionController", Controller)
        assert hooks._execute(base.EXECUTION_SESSION) == 1
        assert hooks.real_order_calls() == 2

    class UnsafeResult(Result):
        unresolved_unknown = True

    class UnsafeController(Controller):
        def execute(self, _plan):
            return UnsafeResult()

    with base.hook_case(tmp_path / "unsafe", mode=Mode.CANARY) as (hooks, _writer, _broker, _accounts):
        hooks._decisions = SimpleNamespace(for_session=lambda _session: (decision,))
        monkeypatch.setattr(
            hooks,
            "_reconcile",
            lambda _kind: (
                SimpleNamespace(reconciliation_id="recon_" + "4" * 64),
                facts.broker_snapshot,
                base.Account(),
            ),
        )
        monkeypatch.setattr(hooks, "_execution_facts", lambda _decision: facts)
        monkeypatch.setattr(hooks, "_require_promotion", lambda _account_hash: None)
        monkeypatch.setattr(hooks, "_capability", lambda _authorities: object())
        monkeypatch.setattr(ps, "LiveExecutionController", UnsafeController)
        with pytest.raises(ps.ProductionServicesUnavailable, match="SAFETY_FAILURE"):
            hooks._execute(base.EXECUTION_SESSION)


def test_cycle_failure_paths_halt_with_specific_blockers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with base.hook_case(tmp_path / "not-ready") as (hooks, _writer, _broker, _accounts):
        with pytest.raises(ps.ProductionServicesUnavailable, match="NOT_READY"):
            hooks.cycle(NOW)

    with base.hook_case(tmp_path / "execute") as (hooks, _writer, _broker, _accounts):
        base.ready(hooks)
        monkeypatch.setattr(hooks, "_execute", lambda _session: (_ for _ in ()).throw(RuntimeError("x")))
        with pytest.raises(RuntimeError, match="x"):
            hooks.cycle(NOW)
        assert "EXECUTION_STEP_FAILED" in hooks.status.blockers

    with base.hook_case(
        tmp_path / "eod",
        status=MarketSessionStatus.CLOSED,
        clock=lambda: base.POST_CLOSE,
    ) as (hooks, _writer, _broker, _accounts):
        base.ready(hooks)
        monkeypatch.setattr(hooks, "_eod", lambda _session: (_ for _ in ()).throw(RuntimeError("eod")))
        with pytest.raises(RuntimeError, match="eod"):
            hooks.cycle(base.POST_CLOSE)
        assert "EOD_RECONCILIATION_FAILED" in hooks.status.blockers

    with base.hook_case(
        tmp_path / "decision",
        status=MarketSessionStatus.CLOSED,
        clock=lambda: base.POST_CLOSE,
    ) as (hooks, _writer, _broker, _accounts):
        base.ready(hooks)
        monkeypatch.setattr(hooks, "_eod", lambda _session: 0)
        monkeypatch.setattr(
            hooks,
            "_post_close_decision",
            lambda _session: (_ for _ in ()).throw(RuntimeError("decision")),
        )
        with pytest.raises(RuntimeError, match="decision"):
            hooks.cycle(base.POST_CLOSE)
        assert "POST_CLOSE_DECISION_FAILED" in hooks.status.blockers


def test_signal_and_builder_validation_branches_are_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ps.signal, "SIGINT", None)
    monkeypatch.setattr(ps.signal, "signal", lambda *_args: (_ for _ in ()).throw(OSError("no")))
    ps._install_stop_handlers(ps._StopFlag())

    settings, config = base.settings_for(tmp_path, Mode.SHADOW)
    with ps.WriterLease.acquire(
        tmp_path / "state" / "firmquant.sqlite3",
        owner="builder-validation",
        clock=lambda: NOW,
    ) as writer:
        with pytest.raises(TypeError, match="config path"):
            ps.build_production_runtime(
                config_path="bad",  # type: ignore[arg-type]
                settings=settings,
                writer=writer,
                clock=lambda: NOW,
            )
        with pytest.raises(TypeError, match="settings"):
            ps.build_production_runtime(
                config_path=config,
                settings=object(),  # type: ignore[arg-type]
                writer=writer,
                clock=lambda: NOW,
            )
        with pytest.raises(TypeError, match="WriterLease"):
            ps.build_production_runtime(
                config_path=config,
                settings=settings,
                writer=object(),  # type: ignore[arg-type]
                clock=lambda: NOW,
            )
        with pytest.raises(TypeError, match="clock"):
            ps.build_production_runtime(
                config_path=config,
                settings=settings,
                writer=writer,
                clock=None,  # type: ignore[arg-type]
            )
        paper = Settings()
        with pytest.raises(ps.ProductionServicesUnavailable, match="SHADOW/CANARY/LIVE"):
            ps.build_production_runtime(
                config_path=config,
                settings=paper,
                writer=writer,
                clock=lambda: NOW,
            )
