from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from uquant.account import save_account
from uquant.data import DataStore
from uquant.types import AccountState

from firmquant.application.operations import (
    LocalOperatorService,
    OperatorCommand,
    OperatorCommandDenied,
    OperatorInteraction,
    OperatorReconciliation,
    OperatorRequest,
    SystemOrderCancellationPort,
    create_local_operator_service,
)
from firmquant.config import Mode
from firmquant.domain.broker_facts import Side
from firmquant.domain.orders import ExecutionIntent
from firmquant.domain.values import Shares, Symbol
from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.database import Database
from firmquant.persistence.repositories import (
    DecisionSnapshotRepository,
    ExecutionLedgerRepository,
)
from firmquant.security.secrets import SecretBytes
from firmquant.strategy.identity import StrategyIdentity
from tests.fixtures.broker_contract import order_event, write_recording
from tests.fixtures.session_cases import decision_snapshot

NOW = datetime(2026, 8, 26, 1, 0, tzinfo=UTC)
FIRMQUANT_COMMIT = "f" * 40


class StaticSecrets:
    def get_secret(self, name: str) -> SecretBytes:
        assert name == "ARM_MAC_KEY"
        return SecretBytes(b"test-only-arm-mac-key-material-32")


class RecordingCanceller:
    def __init__(self, callback: Callable[[tuple[str, ...]], tuple[str, ...]]) -> None:
        self._callback = callback

    def cancel_system_orders(self, broker_order_ids: tuple[str, ...]) -> tuple[str, ...]:
        return self._callback(broker_order_ids)


def interaction(
    phrase: Callable[[str], str] = lambda prompt: prompt.removeprefix("请输入确认短语: "),
    *,
    terminal: bool = True,
    environment: dict[str, str] | None = None,
) -> OperatorInteraction:
    return OperatorInteraction(
        interactive_terminal=terminal,
        confirmation_reader=phrase,
        environment={} if environment is None else environment,
    )


def request(command: OperatorCommand, **changes: object) -> OperatorRequest:
    values: dict[str, object] = {"command": command}
    values.update(changes)
    return OperatorRequest(**values)


def paper_config(path: Path) -> None:
    path.write_text(
        "\n".join(
            (
                "schema_version = 1",
                'mode = "PAPER"',
                "live_trading_enabled = false",
                'timezone = "Asia/Shanghai"',
                "",
                "[broker]",
                'adapter = "PAPER"',
                "",
                "[paths]",
                'state_directory = "state"',
                'data_directory = "data"',
                'report_directory = "reports"',
                'backup_directory = "backups"',
                "",
                "[compliance]",
                "program_trading_report_confirmed = false",
                "broker_api_authorized = false",
                "",
            )
        ),
        encoding="utf-8",
    )


def live_config(path: Path) -> None:
    path.write_text(
        "\n".join(
            (
                "schema_version = 1",
                'mode = "CANARY"',
                "live_trading_enabled = true",
                'timezone = "Asia/Shanghai"',
                "",
                "[broker]",
                'adapter = "XTQUANT"',
                "",
                "[paths]",
                'state_directory = "state"',
                'data_directory = "data"',
                'report_directory = "reports"',
                'backup_directory = "backups"',
                "",
                "[compliance]",
                "program_trading_report_confirmed = true",
                "broker_api_authorized = true",
                "",
                "[canary_caps]",
                'max_order_notional = "1000"',
                'max_daily_submitted_notional = "2000"',
                'max_daily_filled_notional = "2000"',
                'max_symbol_notional = "1500"',
                'max_total_gross_notional = "3000"',
                "",
            )
        ),
        encoding="utf-8",
    )


def service(
    config: Path,
    *,
    runner: Callable[[Mode], dict[str, object]] | None = None,
    reconciler: Callable[[Database], OperatorReconciliation] | None = None,
    reporter: Callable[[date | None, Database], dict[str, object]] | None = None,
    system_order_canceller: SystemOrderCancellationPort | None = None,
) -> LocalOperatorService:
    return LocalOperatorService(
        config_path=config,
        clock=lambda: NOW,
        firmquant_commit_provider=lambda: FIRMQUANT_COMMIT,
        secret_provider=StaticSecrets(),
        runner=runner,
        reconciler=reconciler,
        reporter=reporter,
        system_order_canceller=system_order_canceller,
    )


def initialize(operator: LocalOperatorService) -> None:
    operator.execute(request(OperatorCommand.INIT), interaction())


def prepare_empty_uquant_account(config: Path, *, cash: str = "100000.0000") -> None:
    data_directory = config.parent / "data"
    data_directory.mkdir(exist_ok=True)
    (data_directory / "sz300308.csv").write_text(
        "date,open,high,low,close,volume\n"
        "2026-08-24,10,10.2,9.8,10.1,1000000\n"
        "2026-08-25,10.1,10.3,9.9,10.2,1100000\n",
        encoding="utf-8",
    )
    manifest = DataStore(data_directory).manifest(("sz300308",), as_of="2026-08-25")
    account = AccountState.empty(float(cash))
    account.data_hash = manifest.digest
    account.data_hash_as_of = manifest.end
    account.data_hash_symbols = list(manifest.symbols)
    account.code_hash = StrategyIdentity.locked().economic_code_fingerprint
    save_account(account, config.parent / "state" / "uquant-account.json")


def database_path(config: Path) -> Path:
    return config.parent / "state" / "firmquant.sqlite3"


def seed_live_readiness(config: Path) -> None:
    database = Database.open(database_path(config))
    try:
        with database.transaction():
            database.write(
                """
                INSERT INTO broker_snapshots(
                    snapshot_id, account_id_hash, account_type, session_date, captured_at,
                    broker_event_watermark, raw_payload_sha256, complete
                ) VALUES (?, ?, 'CASH', '2026-08-26', ?, 0, ?, 1)
                """,
                ("snapshot-live", "a" * 64, NOW.isoformat(), "b" * 64),
            )
            database.write(
                """
                INSERT INTO cash_snapshots(snapshot_id, available_cash, total_assets)
                VALUES ('snapshot-live', '100000.0000', '100000.0000')
                """
            )
            database.write(
                """
                INSERT INTO reconciliation_runs(
                    reconciliation_id, kind, strategy_session, started_at, completed_at,
                    passed, blockers_json, details_json, details_sha256
                ) VALUES (?, 'STARTUP', '2026-08-26', ?, ?, 1, '[]', '{}', ?)
                """,
                ("recon_" + "1" * 64, NOW.isoformat(), NOW.isoformat(), "2" * 64),
            )
            database.write(
                """
                INSERT INTO runtime_state(
                    singleton_id, mode, state, revision, reason, blockers_json, updated_at
                ) VALUES (1, 'CANARY', 'READY', 1, 'startup reconciliation passed', '[]', ?)
                """,
                (NOW.isoformat(),),
            )
            AuditLedger(database).append(
                audit_event_id="shadow-ready-proof",
                category="RUNTIME",
                actor="session-coordinator",
                payload={
                    "schema": "firmquant.runtime-transition.v1",
                    "mode": "SHADOW",
                    "state": "READY",
                    "revision": 3,
                    "reason": "shadow startup reconciliation passed",
                    "blockers": (),
                },
                created_at=NOW,
            )
    finally:
        database.close()


def test_init_creates_only_safe_paper_state_and_status_contract(tmp_path: Path) -> None:
    config = tmp_path / "firmquant.toml"
    operator = service(config)

    initialized = operator.execute(request(OperatorCommand.INIT), interaction())
    status = operator.execute(request(OperatorCommand.STATUS), interaction())

    assert initialized.payload["mode"] == "PAPER"
    assert config.is_file()
    assert "live_trading_enabled = false" in config.read_text(encoding="utf-8")
    assert (tmp_path / "var" / "state" / "firmquant.sqlite3").is_file()
    assert status.payload["mode"] == "PAPER"
    assert status.payload["runtime_state"] == "DISARMED"
    assert status.payload["armed"] is False
    assert status.payload["uquant_commit"] == "105695aacd3d1c7e62705f64188da88d202db4cd"
    assert "account" not in json.dumps(dict(status.payload)).casefold()


@pytest.mark.parametrize(
    ("terminal", "environment", "code"),
    [
        (False, {}, "ARM_INTERACTIVE_TERMINAL_REQUIRED"),
        (True, {"CI": "true"}, "ARM_FORBIDDEN_IN_CI"),
    ],
)
def test_arm_live_rejects_non_tty_and_ci_without_persisting_a_lease(
    tmp_path: Path,
    terminal: bool,
    environment: dict[str, str],
    code: str,
) -> None:
    config = tmp_path / "firmquant.toml"
    live_config(config)
    operator = service(config)
    initialize(operator)
    seed_live_readiness(config)

    with pytest.raises(OperatorCommandDenied, match=code):
        operator.execute(
            request(OperatorCommand.ARM_LIVE),
            interaction(terminal=terminal, environment=environment),
        )

    database = Database.open(database_path(config))
    try:
        assert database.scalar("SELECT count(*) FROM arm_leases") == 0
    finally:
        database.close()


def test_arm_live_rejects_unverified_installed_uquant_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "firmquant.toml"
    live_config(config)
    operator = service(config)
    initialize(operator)
    seed_live_readiness(config)

    def reject_identity(_identity: StrategyIdentity) -> None:
        raise RuntimeError("installed package manifest mismatch")

    monkeypatch.setattr(StrategyIdentity, "verify", reject_identity)

    with pytest.raises(OperatorCommandDenied, match="UQUANT_IDENTITY_UNAVAILABLE"):
        operator.execute(request(OperatorCommand.ARM_LIVE), interaction())

    database = Database.open_read_only(database_path(config))
    try:
        assert database.scalar("SELECT count(*) FROM arm_leases") == 0
    finally:
        database.close()


def test_arm_disarm_and_halt_are_durable_and_never_echo_sensitive_identity(
    tmp_path: Path,
) -> None:
    config = tmp_path / "firmquant.toml"
    live_config(config)
    operator = service(config)
    initialize(operator)
    seed_live_readiness(config)

    armed = operator.execute(request(OperatorCommand.ARM_LIVE), interaction())
    rendered = json.dumps(dict(armed.payload))
    assert armed.payload["armed"] is True
    assert armed.payload["mode"] == "CANARY"
    assert "a" * 64 not in rendered
    assert "ARM FIRMQUANT" not in rendered

    halted = operator.execute(
        request(OperatorCommand.HALT, reason="operator emergency stop"),
        interaction(),
    )
    status = operator.execute(request(OperatorCommand.STATUS), interaction())
    assert halted.payload["runtime_state"] == "HALTED"
    assert status.payload["kill_switch"] is True
    assert status.payload["armed"] is False
    assert "KILL_SWITCH" in status.payload["blockers"]

    disarmed = operator.execute(request(OperatorCommand.DISARM), interaction())
    assert disarmed.payload["runtime_state"] == "DISARMED"


def test_status_authenticates_active_lease_and_fails_closed_after_mac_tamper(
    tmp_path: Path,
) -> None:
    config = tmp_path / "firmquant.toml"
    live_config(config)
    operator = service(config)
    initialize(operator)
    seed_live_readiness(config)

    operator.execute(request(OperatorCommand.ARM_LIVE), interaction())
    authenticated = operator.execute(request(OperatorCommand.STATUS), interaction())
    assert authenticated.payload["armed"] is True
    assert authenticated.payload["blockers"] == []

    database = Database.open(database_path(config))
    try:
        with database.transaction():
            database.write("UPDATE arm_leases SET lease_mac = ?", ("0" * 64,))
    finally:
        database.close()

    tampered = operator.execute(request(OperatorCommand.STATUS), interaction())
    assert tampered.payload["armed"] is False
    assert "ARM_LEASE_AUTHENTICATION_FAILED" in tampered.payload["blockers"]


def test_status_does_not_trust_a_lease_when_the_mac_secret_is_unavailable(
    tmp_path: Path,
) -> None:
    config = tmp_path / "firmquant.toml"
    live_config(config)
    operator = service(config)
    initialize(operator)
    seed_live_readiness(config)
    operator.execute(request(OperatorCommand.ARM_LIVE), interaction())

    verifier_without_secret = LocalOperatorService(
        config_path=config,
        clock=lambda: NOW,
        firmquant_commit_provider=lambda: FIRMQUANT_COMMIT,
    )
    status = verifier_without_secret.execute(
        request(OperatorCommand.STATUS),
        interaction(environment={}),
    )

    assert status.payload["armed"] is False
    assert "ARM_LEASE_AUTHENTICATION_FAILED" in status.payload["blockers"]


def test_resume_always_reconciles_before_ready(tmp_path: Path) -> None:
    config = tmp_path / "firmquant.toml"
    live_config(config)
    calls = 0

    def reconcile(_database: Database) -> OperatorReconciliation:
        nonlocal calls
        calls += 1
        return OperatorReconciliation(
            reconciliation_id="recon_" + "3" * 64,
            passed=True,
            blockers=(),
        )

    operator = service(config, reconciler=reconcile)
    initialize(operator)
    seed_live_readiness(config)
    operator.execute(request(OperatorCommand.HALT), interaction())

    resumed = operator.execute(request(OperatorCommand.RESUME), interaction())

    assert calls == 1
    assert resumed.payload["runtime_state"] == "READY"
    assert resumed.payload["reconciliation_id"] == "recon_" + "3" * 64
    assert resumed.payload["armed"] is False


def test_failed_resume_reconciliation_returns_to_halted(tmp_path: Path) -> None:
    config = tmp_path / "firmquant.toml"
    live_config(config)

    def reconcile(_database: Database) -> OperatorReconciliation:
        return OperatorReconciliation(
            reconciliation_id="recon_" + "4" * 64,
            passed=False,
            blockers=("EXTERNAL_BROKER_ORDER",),
        )

    operator = service(config, reconciler=reconcile)
    initialize(operator)
    seed_live_readiness(config)
    operator.execute(request(OperatorCommand.HALT), interaction())

    with pytest.raises(OperatorCommandDenied, match="RECONCILIATION_FAILED"):
        operator.execute(request(OperatorCommand.RESUME), interaction())

    status = operator.execute(request(OperatorCommand.STATUS), interaction())
    assert status.payload["runtime_state"] == "HALTED"
    assert "EXTERNAL_BROKER_ORDER" in status.payload["blockers"]


def test_resume_revokes_an_existing_lease_even_when_halt_came_from_another_path(
    tmp_path: Path,
) -> None:
    config = tmp_path / "firmquant.toml"
    live_config(config)

    def reconcile(_database: Database) -> OperatorReconciliation:
        return OperatorReconciliation(
            reconciliation_id="recon_" + "8" * 64,
            passed=True,
            blockers=(),
        )

    operator = service(config, reconciler=reconcile)
    initialize(operator)
    seed_live_readiness(config)
    operator.execute(request(OperatorCommand.ARM_LIVE), interaction())
    database = Database.open(database_path(config))
    try:
        with database.transaction():
            database.write(
                """
                UPDATE runtime_state
                SET state = 'HALTED', revision = revision + 1,
                    reason = 'external activity halt',
                    blockers_json = '["EXTERNAL_BROKER_ORDER"]', updated_at = ?
                WHERE singleton_id = 1
                """,
                (NOW.isoformat(),),
            )
    finally:
        database.close()

    resumed = operator.execute(request(OperatorCommand.RESUME), interaction())

    assert resumed.payload["runtime_state"] == "READY"
    assert resumed.payload["armed"] is False
    database = Database.open_read_only(database_path(config))
    try:
        assert database.scalar("SELECT count(*) FROM arm_leases WHERE revoked_at IS NULL") == 0
    finally:
        database.close()


@pytest.mark.parametrize("failure", ["exception", "invalid-result"])
def test_resume_reconciliation_faults_fail_back_to_halted(
    tmp_path: Path,
    failure: str,
) -> None:
    config = tmp_path / "firmquant.toml"
    live_config(config)

    def reconcile(_database: Database) -> OperatorReconciliation:
        if failure == "exception":
            raise RuntimeError("untrusted broker payload")
        return object()  # type: ignore[return-value]

    operator = service(config, reconciler=reconcile)
    initialize(operator)
    seed_live_readiness(config)
    operator.execute(request(OperatorCommand.HALT), interaction())

    with pytest.raises(OperatorCommandDenied):
        operator.execute(request(OperatorCommand.RESUME), interaction())

    status = operator.execute(request(OperatorCommand.STATUS), interaction())
    assert status.payload["runtime_state"] == "HALTED"
    assert status.payload["kill_switch"] is True


def test_status_calculates_account_gross_with_exact_decimal_arithmetic(
    tmp_path: Path,
) -> None:
    config = tmp_path / "firmquant.toml"
    paper_config(config)
    operator = service(config)
    initialize(operator)
    database = Database.open(database_path(config))
    try:
        with database.transaction():
            database.write(
                """
                INSERT INTO broker_snapshots(
                    snapshot_id, account_id_hash, account_type, session_date, captured_at,
                    broker_event_watermark, raw_payload_sha256, complete
                ) VALUES ('decimal-snapshot', ?, 'CASH', '2026-08-26', ?, 0, ?, 1)
                """,
                ("a" * 64, NOW.isoformat(), "b" * 64),
            )
            database.write(
                """
                INSERT INTO cash_snapshots(snapshot_id, available_cash, total_assets)
                VALUES ('decimal-snapshot', '0.7', '1')
                """
            )
            for symbol, market_value in (("600519.SH", "0.1"), ("000001.SZ", "0.2")):
                database.write(
                    """
                    INSERT INTO position_snapshots(
                        snapshot_id, symbol, total_shares, sellable_shares,
                        average_cost, market_value
                    ) VALUES ('decimal-snapshot', ?, 1, 1, '0', ?)
                    """,
                    (symbol, market_value),
                )
    finally:
        database.close()

    status = operator.execute(request(OperatorCommand.STATUS), interaction())

    assert status.payload["current_cash"] == "0.7"
    assert status.payload["actual_gross"] == "0.3"


def test_status_reads_one_consistent_sqlite_snapshot_during_concurrent_updates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "firmquant.toml"
    paper_config(config)
    operator = service(config)
    initialize(operator)
    first_read = threading.Event()
    continue_read = threading.Event()
    original = LocalOperatorService._runtime_from_row

    def pause_after_first_read(database: Database, fallback_mode: Mode):
        observed = original(database, fallback_mode)
        first_read.set()
        if not continue_read.wait(timeout=5):
            raise RuntimeError("status concurrency test timed out")
        return observed

    monkeypatch.setattr(
        LocalOperatorService,
        "_runtime_from_row",
        staticmethod(pause_after_first_read),
    )
    results: list[object] = []

    def read_status() -> None:
        try:
            results.append(operator.execute(request(OperatorCommand.STATUS), interaction()))
        except Exception as error:  # pragma: no cover - asserted below
            results.append(error)

    reader = threading.Thread(target=read_status)
    reader.start()
    assert first_read.wait(timeout=5)
    database = Database.open(database_path(config))
    try:
        with database.transaction():
            snapshot = decision_snapshot()
            DecisionSnapshotRepository(database).append(snapshot)
            aggregate = ExecutionLedgerRepository(database).append_intent(
                ExecutionIntent.create(
                    decision_id=snapshot.decision_id,
                    uquant_order_id="O-CONCURRENT-UNKNOWN",
                    symbol=Symbol.parse("600519.SH"),
                    side=Side.BUY,
                    requested_shares=Shares(100),
                    strategy_session=snapshot.strategy_session,
                    uquant_source_sha=snapshot.uquant_commit,
                ),
                created_at=NOW,
            )
            database.write(
                "UPDATE execution_intents SET state = 'UNKNOWN' WHERE execution_id = ?",
                (aggregate.intent.execution_id,),
            )
    finally:
        database.close()
        continue_read.set()
    reader.join(timeout=5)

    assert not reader.is_alive()
    assert len(results) == 1
    result = results[0]
    assert not isinstance(result, Exception)
    assert result.payload["unresolved_orders"] == 0  # type: ignore[union-attr]


def test_cancel_system_orders_passes_only_active_system_mappings_to_capability_port(
    tmp_path: Path,
) -> None:
    config = tmp_path / "firmquant.toml"
    paper_config(config)
    observed: list[tuple[str, ...]] = []

    def cancel(order_ids: tuple[str, ...]) -> tuple[str, ...]:
        observed.append(order_ids)
        return order_ids

    operator = service(config, system_order_canceller=RecordingCanceller(cancel))
    initialize(operator)
    database = Database.open(database_path(config))
    try:
        with database.transaction():
            snapshot = decision_snapshot()
            DecisionSnapshotRepository(database).append(snapshot)
            aggregate = ExecutionLedgerRepository(database).append_intent(
                ExecutionIntent.create(
                    decision_id=snapshot.decision_id,
                    uquant_order_id="O-CANCEL-1",
                    symbol=Symbol.parse("600519.SH"),
                    side=Side.BUY,
                    requested_shares=Shares(100),
                    strategy_session=snapshot.strategy_session,
                    uquant_source_sha=snapshot.uquant_commit,
                ),
                created_at=NOW,
            )
            for order_id, execution_id, ownership, status in (
                (
                    "system-active",
                    aggregate.intent.execution_id,
                    "SYSTEM",
                    "ACKNOWLEDGED",
                ),
                ("system-filled", None, "SYSTEM", "FILLED"),
                ("manual-active", None, "EXTERNAL", "ACKNOWLEDGED"),
            ):
                database.write(
                    """
                    INSERT INTO broker_orders(
                        broker_order_id, execution_id, ownership, client_order_id, symbol,
                        side, status, requested_shares, filled_shares, limit_price,
                        session_date, last_event_sequence, event_time, received_at,
                        raw_payload_sha256
                    ) VALUES (?, ?, ?, NULL, '600519.SH', 'BUY', ?, 100, 0, '10.00',
                              '2026-08-26', 1, ?, ?, ?)
                    """,
                    (
                        order_id,
                        execution_id,
                        ownership,
                        status,
                        NOW.isoformat(),
                        NOW.isoformat(),
                        hashlib.sha256(order_id.encode()).hexdigest(),
                    ),
                )
    finally:
        database.close()

    result = operator.execute(request(OperatorCommand.CANCEL_SYSTEM_ORDERS), interaction())

    assert observed == [("system-active",)]
    assert result.payload["cancelled_order_ids"] == ["system-active"]


def test_cancel_system_orders_requires_the_capability_bound_port(tmp_path: Path) -> None:
    config = tmp_path / "firmquant.toml"
    paper_config(config)
    operator = service(config)
    initialize(operator)

    with pytest.raises(OperatorCommandDenied, match="WRITE_CAPABILITY_UNAVAILABLE"):
        operator.execute(request(OperatorCommand.CANCEL_SYSTEM_ORDERS), interaction())


def test_real_cancel_rejects_a_non_capability_paper_port(tmp_path: Path) -> None:
    config = tmp_path / "firmquant.toml"
    live_config(config)
    operator = service(
        config,
        system_order_canceller=RecordingCanceller(lambda order_ids: order_ids),
    )
    initialize(operator)

    with pytest.raises(OperatorCommandDenied, match="WRITE_CAPABILITY_UNAVAILABLE"):
        operator.execute(request(OperatorCommand.CANCEL_SYSTEM_ORDERS), interaction())


def test_run_and_manual_reconciliation_delegate_to_typed_ports(tmp_path: Path) -> None:
    config = tmp_path / "firmquant.toml"
    paper_config(config)
    modes: list[Mode] = []
    reconciliation_calls = 0

    def run(mode: Mode) -> dict[str, object]:
        modes.append(mode)
        return {"mode": mode.value, "runtime_state": "READY"}

    def reconcile(_database: Database) -> OperatorReconciliation:
        nonlocal reconciliation_calls
        reconciliation_calls += 1
        return OperatorReconciliation(
            reconciliation_id="recon_" + "5" * 64,
            passed=True,
            blockers=(),
        )

    operator = service(config, runner=run, reconciler=reconcile)
    initialize(operator)

    run_result = operator.execute(
        request(OperatorCommand.RUN, mode=Mode.PAPER),
        interaction(),
    )
    reconciliation = operator.execute(request(OperatorCommand.RECONCILE), interaction())

    assert modes == [Mode.PAPER]
    assert run_result.payload["runtime_state"] == "READY"
    assert reconciliation_calls == 1
    assert reconciliation.payload["passed"] is True
    assert reconciliation.payload["runtime_state"] == "READY"


def test_default_composition_runs_safe_paper_startup_and_reconciliation(
    tmp_path: Path,
) -> None:
    config = tmp_path / "firmquant.toml"
    paper_config(config)
    operator = create_local_operator_service(config)
    assert isinstance(operator, LocalOperatorService)
    initialize(operator)
    prepare_empty_uquant_account(config)

    result = operator.execute(
        request(OperatorCommand.RUN, mode=Mode.PAPER),
        interaction(),
    )

    assert result.payload["mode"] == "PAPER"
    assert result.payload["runtime_state"] == "READY"
    assert result.payload["reconciliation_passed"] is True
    assert result.payload["real_order_calls"] == 0
    database = Database.open_read_only(database_path(config))
    try:
        assert database.scalar("SELECT count(*) FROM broker_snapshots") == 1
        assert database.scalar("SELECT count(*) FROM reconciliation_runs") == 1
        assert database.scalar("SELECT state FROM runtime_state WHERE singleton_id = 1") == "READY"
    finally:
        database.close()


def test_manual_reconciliation_cannot_bypass_explicit_resume_from_halted(
    tmp_path: Path,
) -> None:
    config = tmp_path / "firmquant.toml"
    paper_config(config)

    def reconcile(_database: Database) -> OperatorReconciliation:
        return OperatorReconciliation(
            reconciliation_id="recon_" + "6" * 64,
            passed=True,
            blockers=(),
        )

    operator = service(config, reconciler=reconcile)
    initialize(operator)
    database = Database.open(database_path(config))
    try:
        with database.transaction():
            database.write(
                """
                INSERT INTO runtime_state(
                    singleton_id, mode, state, revision, reason, blockers_json, updated_at
                ) VALUES (1, 'PAPER', 'HALTED', 1, 'reconciliation mismatch',
                          '["RECONCILIATION_MISMATCH"]', ?)
                """,
                (NOW.isoformat(),),
            )
    finally:
        database.close()

    result = operator.execute(request(OperatorCommand.RECONCILE), interaction())
    status = operator.execute(request(OperatorCommand.STATUS), interaction())

    assert result.payload["passed"] is True
    assert result.exit_code == 2
    assert result.payload["runtime_state"] == "HALTED"
    assert status.payload["runtime_state"] == "HALTED"
    assert status.payload["kill_switch"] is False
    assert "EXPLICIT_RESUME_REQUIRED" in status.payload["blockers"]

    resumed = operator.execute(request(OperatorCommand.RESUME), interaction())
    assert resumed.payload["runtime_state"] == "READY"
    database = Database.open_read_only(database_path(config))
    try:
        assert database.scalar("SELECT count(*) FROM risk_events WHERE code = 'KILL_SWITCH_RESET'") == 0
        assert (
            database.scalar(
                "SELECT count(*) FROM audit_events WHERE category = 'OPERATOR' "
                "AND json_extract(payload_json, '$.command') = 'resume'"
            )
            == 1
        )
    finally:
        database.close()


def test_failed_manual_reconciliation_halts_without_falsely_tripping_kill_switch(
    tmp_path: Path,
) -> None:
    config = tmp_path / "firmquant.toml"
    paper_config(config)

    def reconcile(_database: Database) -> OperatorReconciliation:
        return OperatorReconciliation(
            reconciliation_id="recon_" + "7" * 64,
            passed=False,
            blockers=("CASH_MISMATCH",),
        )

    operator = service(config, reconciler=reconcile)
    initialize(operator)

    result = operator.execute(request(OperatorCommand.RECONCILE), interaction())
    status = operator.execute(request(OperatorCommand.STATUS), interaction())

    assert result.exit_code == 2
    assert result.payload["runtime_state"] == "HALTED"
    assert result.payload["blockers"] == ["CASH_MISMATCH"]
    assert status.payload["kill_switch"] is False
    assert status.payload["blockers"] == ["CASH_MISMATCH"]


def test_query_and_report_commands_read_through_application_ports(tmp_path: Path) -> None:
    config = tmp_path / "firmquant.toml"
    paper_config(config)
    report_sessions: list[date | None] = []

    def report(session: date | None, _database: Database) -> dict[str, object]:
        report_sessions.append(session)
        return {"session": None if session is None else session.isoformat(), "healthy": True}

    operator = service(config, reporter=report)
    initialize(operator)
    session = date(2026, 8, 26)

    decisions = operator.execute(request(OperatorCommand.DECISIONS), interaction())
    orders = operator.execute(request(OperatorCommand.ORDERS), interaction())
    fills = operator.execute(request(OperatorCommand.FILLS), interaction())
    reported = operator.execute(
        request(OperatorCommand.REPORT, session=session),
        interaction(),
    )

    assert decisions.payload == {"count": 0, "decisions": []}
    assert orders.payload == {"count": 0, "orders": []}
    assert fills.payload == {"count": 0, "fills": []}
    assert report_sessions == [session]
    assert reported.payload["healthy"] is True


def test_backup_and_verify_backup_commands_form_a_complete_round_trip(tmp_path: Path) -> None:
    config = tmp_path / "firmquant.toml"
    operator = service(config)
    initialize(operator)

    receipt = operator.execute(request(OperatorCommand.BACKUP), interaction())
    bundle = tmp_path / "var" / "backups" / str(receipt.payload["bundle"])
    verification = operator.execute(
        request(OperatorCommand.VERIFY_BACKUP, bundle_path=bundle),
        interaction(),
    )

    assert receipt.payload["backup_id"] == verification.payload["backup_id"]
    assert receipt.payload["manifest_sha256"] == verification.payload["manifest_sha256"]
    assert verification.payload["verified"] is True


def test_replay_command_replays_frozen_events_with_zero_write_attempts(tmp_path: Path) -> None:
    config = tmp_path / "firmquant.toml"
    paper_config(config)
    operator = service(config)
    initialize(operator)
    recording = tmp_path / "events.jsonl"
    write_recording(recording, [order_event()])

    first = operator.execute(
        request(OperatorCommand.REPLAY, events_path=recording),
        interaction(),
    )
    second = operator.execute(
        request(OperatorCommand.REPLAY, events_path=recording),
        interaction(),
    )

    assert first.payload == second.payload
    assert first.payload["event_count"] == 1
    assert first.payload["write_attempts"] == 0


def test_doctor_returns_all_required_checks_without_opening_write_authority(
    tmp_path: Path,
) -> None:
    config = tmp_path / "firmquant.toml"
    operator = service(config)
    initialize(operator)

    result = operator.execute(request(OperatorCommand.DOCTOR), interaction())

    assert len(result.payload["checks"]) == 15
    assert result.exit_code == 2
    assert "account_id" not in json.dumps(dict(result.payload)).casefold()


def test_default_composition_doctor_uses_a_read_only_paper_broker(
    tmp_path: Path,
) -> None:
    config = tmp_path / "firmquant.toml"
    paper_config(config)
    operator = create_local_operator_service(config)
    operator.execute(request(OperatorCommand.INIT), interaction())
    prepare_empty_uquant_account(config)

    result = operator.execute(request(OperatorCommand.DOCTOR), interaction())

    checks = {str(item["name"]): item for item in result.payload["checks"]}
    assert checks["broker-client"]["passed"] is True
    assert checks["readonly-account"]["passed"] is True
    assert checks["live-mode-lock"]["details"]["write_capability"] is False


def test_default_composition_cancel_is_a_safe_noop_without_paper_orders(
    tmp_path: Path,
) -> None:
    config = tmp_path / "firmquant.toml"
    paper_config(config)
    operator = create_local_operator_service(config)
    operator.execute(request(OperatorCommand.INIT), interaction())

    result = operator.execute(
        request(OperatorCommand.CANCEL_SYSTEM_ORDERS),
        interaction(),
    )

    assert result.payload == {
        "requested_order_ids": [],
        "cancelled_order_ids": [],
    }
