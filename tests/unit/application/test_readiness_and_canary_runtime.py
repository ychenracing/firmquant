from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from firmquant.application import execution_evidence_runtime as evidence_runtime
from firmquant.application import live_readiness_runtime as readiness_runtime
from firmquant.application.execution_evidence import BlockerCode
from firmquant.application.execution_evidence_runtime import RuntimeEvidenceError
from firmquant.application.production_identity import (
    DeploymentIdentity,
    OperationalEvidenceIdentity,
)
from firmquant.config import Mode, Settings
from firmquant.domain.broker_facts import AccountType, BrokerAccountFact, BrokerSnapshot
from firmquant.domain.values import Money
from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.database import Database

NOW = datetime(2026, 8, 28, 1, 0, tzinfo=UTC)
SESSION = date(2026, 8, 28)
ACCOUNT = "a" * 64


def _snapshot(*, account_hash: str = ACCOUNT) -> BrokerSnapshot:
    return BrokerSnapshot(
        snapshot_id="snapshot-eod",
        account=BrokerAccountFact(
            account_id_hash=account_hash,
            account_type=AccountType.CASH,
            available_cash=Money(Decimal("10000")),
            total_assets=Money(Decimal("10000")),
        ),
        positions=(),
        orders=(),
        fills=(),
        session_date=SESSION,
        captured_at=NOW,
        broker_event_watermark=0,
        raw_payload_sha256="f" * 64,
        complete=True,
    )


def _canary_payload() -> dict[str, object]:
    return {
        "schema": "firmquant.canary-plan-evidence.v1",
        "execution_session": SESSION.isoformat(),
        "strategy_session": SESSION.isoformat(),
        "decision_id": "decision-canary",
        "plan_id": "plan-canary",
        "firmquant_commit": "1" * 40,
        "uquant_commit": "2" * 40,
        "promotion_config_sha256": "3" * 64,
        "account_sha256": ACCOUNT,
        "data_sha256": "4" * 64,
        "calendar_sha256": "5" * 64,
        "portfolio_equity": "10000",
        "orders": [
            {
                "uquant_order_id": "uq-missing",
                "symbol": "600000.SH",
                "side": "BUY",
                "planned_shares": 100,
                "reference_price": "10",
            }
        ],
        "blockers": [
            {
                "uquant_order_id": "uq-blocked",
                "symbol": "000001.SZ",
                "reason_code": "TARGET_ALREADY_SATISFIED",
            }
        ],
        "targets": [
            {
                "symbol": "600000.SH",
                "target_shares": 100,
                "target_weight": "0.10",
                "reference_price": "10",
            }
        ],
        "created_at": NOW.isoformat(),
    }


def _canary_operational_identity() -> OperationalEvidenceIdentity:
    deployment = DeploymentIdentity(
        firmquant_commit="1" * 40,
        uquant_commit="2" * 40,
        uquant_tree="6" * 40,
        uquant_package_manifest_sha256="7" * 64,
        uquant_code_fingerprint="8" * 64,
        uquant_config_fingerprint="9" * 64,
        semantic_config_sha256="3" * 64,
        raw_config_sha256="a" * 64,
        xtquant_safety_manifest_sha256="b" * 64,
        account_id_hash=ACCOUNT,
        account_authority_epoch=2,
        mode_epoch=3,
        mode=Mode.CANARY,
        caps_sha256="c" * 64,
        production_policy_sha256="d" * 64,
    )
    return OperationalEvidenceIdentity(
        deployment_identity=deployment,
        account_state_sha256="e" * 64,
        broker_snapshot_id="snapshot-eod",
        broker_snapshot_sha256="f" * 64,
        broker_event_watermark=0,
        snapshot_started_at=NOW,
        snapshot_completed_at=NOW,
        snapshot_duration_ms=17,
        calendar_sha256="5" * 64,
        active_data_generation_sha256="4" * 64,
        strategy_data_manifest_sha256="4" * 64,
        strategy_session=SESSION,
        decision_id="decision-canary",
        phase="EOD",
        kind="CANARY_EXECUTION",
    )


def test_collect_live_readiness_empty_authority_surface_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "firmquant.toml"
    config.write_text("schema_version = 1\n", encoding="utf-8")
    settings = Settings()
    database = Database.open(tmp_path / "ledger.sqlite3")
    try:
        source = SimpleNamespace(uquant_commit="2" * 40, config_fingerprint="3" * 64)
        monkeypatch.setattr(readiness_runtime, "load_locked_source_identity", lambda: source)
        monkeypatch.setattr(readiness_runtime, "current_clean_firmquant_commit", lambda: "1" * 40)

        class Identity:
            def verify(self) -> None:
                return None

        monkeypatch.setattr(readiness_runtime.StrategyIdentity, "locked", lambda: Identity())
        snapshot = readiness_runtime.collect_live_readiness(
            settings=settings,
            config_path=config,
            database=database,
            now=NOW,
        )
        assert snapshot.passed is False
        assert snapshot.software_ready is False
        assert snapshot.firmquant_commit == "1" * 40
        assert snapshot.account_hash is None
        assert snapshot.shadow_sessions == 0
        assert snapshot.canary_sessions == 0
        assert snapshot.heartbeat_age_seconds is None
        assert snapshot.armed is False
        assert snapshot.kill_switch is False
        assert "ACCOUNT_BINDING_MISSING" in snapshot.blockers
        assert list(snapshot.payload()["blockers"]) == list(snapshot.blockers)
    finally:
        database.close()


def test_live_readiness_helpers_parse_evidence_and_fail_closed(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "ledger.sqlite3")
    try:
        assert readiness_runtime._json_mapping({"ok": 1}) == {"ok": 1}
        assert readiness_runtime._json_mapping({1: "bad"}) is None
        missing = tmp_path / "missing"
        assert readiness_runtime._file_sha256(missing) is None
        existing = tmp_path / "identity.json"
        existing.write_text("identity", encoding="utf-8")
        assert readiness_runtime._file_sha256(existing) is not None
        assert readiness_runtime._latest_account_hash(database) is None
        assert readiness_runtime._latest_reconciliation_passed(database, "STARTUP") is False
        assert readiness_runtime._heartbeat(database, NOW) == (False, False, None)
        assert readiness_runtime._kill_switch(database) is False
        assert readiness_runtime._armed(database, NOW) is False
        assert readiness_runtime._latest_backup_matches(
            database,
            firmquant_commit="1" * 40,
            uquant_commit="2" * 40,
            config_sha256="3" * 64,
            account_sha256=ACCOUNT,
            calendar_sha256="4" * 64,
            active_data_manifest_sha256="5" * 64,
            strategy_data_manifest_sha256="6" * 64,
        ) == (False, None)
    finally:
        database.close()


def test_live_readiness_heartbeat_and_backup_identity_match(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "ledger.sqlite3")
    try:
        with database.transaction():
            database.write(
                "INSERT INTO production_heartbeat("
                "singleton_id,mode,runtime_state,observed_at,host_hash,process_id,writer_generation,"
                "broker_connected,broker_read_healthy,broker_write_healthy,pending_events,"
                "control_request_state,processed_events,decisions,executions,eod"
                ") VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "SHADOW",
                    "READY",
                    (NOW - timedelta(seconds=5)).isoformat(),
                    "h" * 64,
                    1,
                    1,
                    1,
                    1,
                    1,
                    0,
                    "IDLE",
                    0,
                    0,
                    0,
                    0,
                ),
            )
        assert readiness_runtime._heartbeat(database, NOW) == (True, True, 5.0)
        with database.transaction():
            database.write(
                "UPDATE production_heartbeat SET observed_at=?,control_request_state=? WHERE singleton_id=1",
                ((NOW + timedelta(seconds=1)).isoformat(), "HALT_REQUESTED"),
            )
        assert readiness_runtime._heartbeat(database, NOW) == (False, False, -1.0)

        deployment = {
            "firmquant_commit": "1" * 40,
            "uquant_commit": "2" * 40,
            "config_sha256": "3" * 64,
            "account_sha256": ACCOUNT,
            "calendar_sha256": "4" * 64,
            "active_data_manifest_sha256": "5" * 64,
            "strategy_data_manifest_sha256": "6" * 64,
        }
        manifest = json.dumps({"schema_version": 2, "deployment": deployment}, sort_keys=True)
        with database.transaction():
            database.write(
                "INSERT INTO backup_receipts("
                "backup_id,database_sha256,account_state_sha256,manifest_json,manifest_sha256,"
                "created_at,verified_at,verification_status"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (
                    "backup-1",
                    "d" * 64,
                    "e" * 64,
                    manifest,
                    "f" * 64,
                    NOW.isoformat(),
                    NOW.isoformat(),
                    "VERIFIED",
                ),
            )
        assert readiness_runtime._latest_backup_matches(
            database,
            firmquant_commit="1" * 40,
            uquant_commit="2" * 40,
            config_sha256="3" * 64,
            account_sha256=ACCOUNT,
            calendar_sha256="4" * 64,
            active_data_manifest_sha256="5" * 64,
            strategy_data_manifest_sha256="6" * 64,
        ) == (True, "backup-1")
    finally:
        database.close()


def test_readiness_helper_branch_matrix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = Database.open(tmp_path / "ledger.sqlite3")
    try:
        reconciliation_rows = iter(
            (
                None,
                {"passed": 0, "blockers_json": "[]"},
                {"passed": 1, "blockers_json": "{"},
                {"passed": 1, "blockers_json": '["BLOCKED"]'},
                {"passed": 1, "blockers_json": "[]"},
            )
        )
        monkeypatch.setattr(Database, "query_one", lambda self, sql, parameters=(): next(reconciliation_rows))
        assert readiness_runtime._latest_reconciliation_passed(database, "STARTUP") is False
        assert readiness_runtime._latest_reconciliation_passed(database, "STARTUP") is False
        assert readiness_runtime._latest_reconciliation_passed(database, "STARTUP") is False
        assert readiness_runtime._latest_reconciliation_passed(database, "STARTUP") is False
        assert readiness_runtime._latest_reconciliation_passed(database, "STARTUP") is True

        heartbeat_rows = iter(
            (
                {"observed_at": "bad", "control_request_state": "IDLE"},
                {"observed_at": "2026-08-28T01:00:00", "control_request_state": "IDLE"},
                {
                    "observed_at": (NOW - timedelta(seconds=31)).isoformat(),
                    "control_request_state": "IDLE",
                },
                {
                    "observed_at": (NOW - timedelta(seconds=5)).isoformat(),
                    "control_request_state": "HALT_REQUESTED",
                },
            )
        )
        monkeypatch.setattr(Database, "query_one", lambda self, sql, parameters=(): next(heartbeat_rows))
        assert readiness_runtime._heartbeat(database, NOW) == (False, False, None)
        assert readiness_runtime._heartbeat(database, NOW) == (False, False, None)
        assert readiness_runtime._heartbeat(database, NOW) == (False, True, 31.0)
        assert readiness_runtime._heartbeat(database, NOW) == (True, False, 5.0)

        arm_rows = iter(
            (
                {"expires_at": "bad"},
                {"expires_at": "2026-08-28T02:00:00"},
                {"expires_at": (NOW - timedelta(seconds=1)).isoformat()},
                {"expires_at": (NOW + timedelta(seconds=1)).isoformat()},
            )
        )
        monkeypatch.setattr(Database, "query_one", lambda self, sql, parameters=(): next(arm_rows))
        assert readiness_runtime._armed(database, NOW) is False
        assert readiness_runtime._armed(database, NOW) is False
        assert readiness_runtime._armed(database, NOW) is False
        assert readiness_runtime._armed(database, NOW) is True

        backup_rows = (
            {"backup_id": "skip-status", "manifest_json": "{}", "verification_status": "FAILED"},
            {"backup_id": "skip-json", "manifest_json": "{", "verification_status": "VERIFIED"},
            {"backup_id": "skip-root", "manifest_json": "[]", "verification_status": "VERIFIED"},
            {
                "backup_id": "skip-version",
                "manifest_json": json.dumps({"schema_version": 1}),
                "verification_status": "VERIFIED",
            },
            {
                "backup_id": "skip-deployment",
                "manifest_json": json.dumps({"schema_version": 2, "deployment": []}),
                "verification_status": "VERIFIED",
            },
            {
                "backup_id": "skip-mismatch",
                "manifest_json": json.dumps({"schema_version": 2, "deployment": {}}),
                "verification_status": "VERIFIED",
            },
        )
        monkeypatch.setattr(Database, "query_all", lambda self, sql, parameters=(): backup_rows)
        assert readiness_runtime._latest_backup_matches(
            database,
            firmquant_commit="1" * 40,
            uquant_commit="2" * 40,
            config_sha256="3" * 64,
            account_sha256=ACCOUNT,
            calendar_sha256="4" * 64,
            active_data_manifest_sha256="5" * 64,
            strategy_data_manifest_sha256="6" * 64,
        ) == (False, None)
    finally:
        database.close()


def test_collect_live_readiness_covers_positive_identity_and_promotion_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "firmquant.toml"
    config.write_text("schema_version = 1\n", encoding="utf-8")
    settings = Settings(paths={"uquant_source_checkout": Path("uquant")})
    database = Database.open(tmp_path / "ledger.sqlite3")
    source = SimpleNamespace(uquant_commit="2" * 40, config_fingerprint="3" * 64)

    class Identity:
        def verify(self) -> None:
            return None

    class GenerationStore:
        def __init__(self, path: Path) -> None:
            self.path = path

        def active(self) -> SimpleNamespace:
            return SimpleNamespace(manifest_sha256="7" * 64)

    class Aggregate:
        def __init__(self, observed_sessions: int) -> None:
            self.observed_sessions = observed_sessions

    class Store:
        def __init__(self, database: Database) -> None:
            self.database = database

        def aggregate(self, *, stage: object, **kwargs: object) -> Aggregate:
            return Aggregate(20 if stage is readiness_runtime.EvidenceStage.SHADOW else 3)

        def qualifies(self, **kwargs: object) -> bool:
            return True

    def query_one(self: Database, sql: str, parameters: object = ()) -> dict[str, object] | None:
        if "account_bindings" in sql:
            return {"account_id_hash": ACCOUNT, "uquant_commit": "2" * 40}
        if "decision_snapshots" in sql:
            return {
                "uquant_commit": "2" * 40,
                "uquant_config_fingerprint": "3" * 64,
                "data_manifest_sha256": "6" * 64,
                "firmquant_commit": "1" * 40,
            }
        return None

    try:
        monkeypatch.setattr(readiness_runtime, "load_locked_source_identity", lambda: source)
        monkeypatch.setattr(readiness_runtime, "current_clean_firmquant_commit", lambda: "1" * 40)
        monkeypatch.setattr(readiness_runtime.StrategyIdentity, "locked", lambda: Identity())
        monkeypatch.setattr(readiness_runtime, "verify_uquant_source_checkout", lambda source, path: None)
        monkeypatch.setattr(readiness_runtime, "_latest_account_hash", lambda database: ACCOUNT)
        monkeypatch.setattr(readiness_runtime, "_file_sha256", lambda path: "6" * 64)
        monkeypatch.setattr(readiness_runtime, "DataGenerationStore", GenerationStore)
        monkeypatch.setattr(
            readiness_runtime,
            "load_trading_calendar_manifest",
            lambda path: SimpleNamespace(
                covered_from=SESSION - timedelta(days=1),
                covered_through=SESSION + timedelta(days=1),
            ),
        )
        monkeypatch.setattr(readiness_runtime, "PromotionStore", Store)
        monkeypatch.setattr(readiness_runtime, "_heartbeat", lambda database, now: (True, True, 1.0))
        monkeypatch.setattr(readiness_runtime, "_latest_reconciliation_passed", lambda database, kind: True)
        monkeypatch.setattr(
            readiness_runtime, "_latest_backup_matches", lambda database, **kwargs: (True, "backup-1")
        )
        monkeypatch.setattr(readiness_runtime, "_kill_switch", lambda database: False)
        monkeypatch.setattr(readiness_runtime, "_armed", lambda database, now: True)
        monkeypatch.setattr(Database, "query_one", query_one)
        monkeypatch.setattr(Database, "scalar", lambda self, sql, parameters=(): 0)

        snapshot = readiness_runtime.collect_live_readiness(
            settings=settings,
            config_path=config,
            database=database,
            now=NOW,
        )
        assert snapshot.firmquant_commit == "1" * 40
        assert snapshot.account_hash == ACCOUNT
        assert snapshot.data_sha256 == "6" * 64
        assert snapshot.calendar_sha256 == "6" * 64
        assert snapshot.backup_id == "backup-1"
        assert snapshot.shadow_sessions == 20
        assert snapshot.canary_sessions == 3
        assert snapshot.heartbeat_age_seconds == 1.0
        assert snapshot.armed is True
    finally:
        database.close()


def test_collect_live_readiness_rejects_invalid_call_contract(tmp_path: Path) -> None:
    config = tmp_path / "firmquant.toml"
    config.write_text("schema_version = 1\n", encoding="utf-8")
    database = Database.open(tmp_path / "ledger.sqlite3")
    try:
        with pytest.raises(TypeError, match="Settings"):
            readiness_runtime.collect_live_readiness(
                settings=object(),  # type: ignore[arg-type]
                config_path=config,
                database=database,
                now=NOW,
            )
        with pytest.raises(TypeError, match="Database"):
            readiness_runtime.collect_live_readiness(
                settings=Settings(),
                config_path=config,
                database=object(),  # type: ignore[arg-type]
                now=NOW,
            )
        with pytest.raises(ValueError, match="timezone-aware"):
            readiness_runtime.collect_live_readiness(
                settings=Settings(),
                config_path=config,
                database=database,
                now=NOW.replace(tzinfo=None),
            )
    finally:
        database.close()


def test_canary_missing_execution_becomes_unknown_observation(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "ledger.sqlite3")
    try:
        with database.transaction():
            AuditLedger(database).append(
                audit_event_id="canary-plan:plan-canary",
                category="CANARY_PLAN_EVIDENCE",
                actor="execution-evidence",
                payload=_canary_payload(),
                created_at=NOW,
            )
        observed = evidence_runtime.finalize_canary_observation(
            database=database,
            eod_snapshot=_snapshot(),
            session=SESSION,
            created_at=NOW,
            operational_identity=_canary_operational_identity(),
        )
        assert observed is not None
        assert observed.unknown_count == 1
        assert observed.submit_count == 0
        assert observed.cancel_count == 0
        assert observed.external_activity == 0
        assert observed.fills == ()
        assert observed.planned_orders[0].blocker is BlockerCode.UNKNOWN
        assert observed.planning_blockers[0].reason_code == "TARGET_ALREADY_SATISFIED"
        assert observed.targets[0].target_shares == 100
    finally:
        database.close()


@pytest.mark.parametrize(
    "identity",
    [
        replace(_canary_operational_identity(), strategy_session=date(2020, 1, 1)),
        replace(_canary_operational_identity(), phase="READINESS"),
        replace(_canary_operational_identity(), kind="BACKUP"),
    ],
)
def test_canary_operational_context_must_match_durable_plan(
    tmp_path: Path,
    identity: OperationalEvidenceIdentity,
) -> None:
    database = Database.open(tmp_path / "ledger.sqlite3")
    try:
        with database.transaction():
            AuditLedger(database).append(
                audit_event_id="canary-plan:plan-canary",
                category="CANARY_PLAN_EVIDENCE",
                actor="execution-evidence",
                payload=_canary_payload(),
                created_at=NOW,
            )
        with pytest.raises(RuntimeEvidenceError, match=r"strategy session|phase|kind"):
            evidence_runtime.finalize_canary_observation(
                database=database,
                eod_snapshot=_snapshot(),
                session=SESSION,
                created_at=NOW,
                operational_identity=identity,
            )
    finally:
        database.close()


def test_canary_finalize_full_execution_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = Database.open(tmp_path / "ledger.sqlite3")

    def query_one(self: Database, sql: str, parameters: object = ()) -> dict[str, object] | None:
        if "FROM execution_intents" in sql:
            return {"execution_id": "exec_canary", "filled_shares": 50, "state": "REJECTED"}
        return None

    def query_all(self: Database, sql: str, parameters: object = ()) -> tuple[dict[str, object], ...]:
        if "FROM fills WHERE execution_id" in sql:
            return (
                {
                    "broker_fill_id": "fill-canary",
                    "symbol": "600000.SH",
                    "side": "BUY",
                    "shares": 50,
                    "price": "10.1",
                    "commission": "5",
                    "stamp_duty": "0",
                    "transfer_fee": "0.1",
                },
            )
        return ()

    def scalar(self: Database, sql: str, parameters: object = ()) -> int:
        if "command_kind='SUBMIT'" in sql:
            return 1
        if "command_kind='CANCEL'" in sql:
            return 2
        return 0

    try:
        monkeypatch.setattr(
            evidence_runtime, "_load_canary_plan", lambda database, session: _canary_payload()
        )
        monkeypatch.setattr(Database, "query_one", query_one)
        monkeypatch.setattr(Database, "query_all", query_all)
        monkeypatch.setattr(Database, "scalar", scalar)
        observed = evidence_runtime.finalize_canary_observation(
            database=database,
            eod_snapshot=_snapshot(),
            session=SESSION,
            created_at=NOW,
            operational_identity=_canary_operational_identity(),
        )
        assert observed is not None
        assert observed.submit_count == 1
        assert observed.cancel_count == 2
        assert observed.rejection_count == 1
        assert observed.unknown_count == 0
        assert observed.planned_orders[0].filled_shares == 50
        assert observed.planned_orders[0].blocker is BlockerCode.VOLUME_LIMIT
        assert observed.fills[0].shares == 50
        assert observed.fills[0].slippage == Decimal("5.0")
    finally:
        database.close()


def test_canary_decoder_and_blocker_edges_fail_closed(tmp_path: Path) -> None:
    assert evidence_runtime._blocker_from_reason(None) is None
    assert evidence_runtime._blocker_from_reason("CASH_INSUFFICIENT") is BlockerCode.INSUFFICIENT_CASH
    assert evidence_runtime._blocker_from_reason("VOLUME_CAPACITY_EXHAUSTED") is BlockerCode.VOLUME_LIMIT
    assert evidence_runtime._blocker_from_reason("UPPER_LIMIT_BUY_BLOCKED") is BlockerCode.PRICE_LIMIT
    assert evidence_runtime._blocker_from_reason("INSTRUMENT_NOT_TRADING") is BlockerCode.NON_TRADABLE
    assert evidence_runtime._blocker_from_reason("MARKET_FACT_SESSION_MISMATCH") is BlockerCode.STALE_QUOTE
    assert evidence_runtime._blocker_from_reason("unexpected") is BlockerCode.UNKNOWN
    assert evidence_runtime._blocker_from_reason("FILLED", partial=True) is BlockerCode.VOLUME_LIMIT
    with pytest.raises(RuntimeEvidenceError):
        evidence_runtime._plan_blockers({})
    with pytest.raises(RuntimeEvidenceError):
        evidence_runtime._plan_blockers({"blockers": ["bad"]})
    with pytest.raises(RuntimeEvidenceError):
        evidence_runtime._plan_targets({})
    with pytest.raises(RuntimeEvidenceError):
        evidence_runtime._plan_targets({"targets": ["bad"]})
    with pytest.raises(RuntimeEvidenceError):
        evidence_runtime._plan_targets({"targets": [{}]})
    with pytest.raises(RuntimeEvidenceError):
        evidence_runtime._plan_orders({"orders": ["bad"]})
    assert evidence_runtime._plan_orders({"orders": [{}]}) == ({},)

    database = Database.open(tmp_path / "ledger.sqlite3")
    try:
        assert evidence_runtime._load_canary_plan(database, session=SESSION) is None
        bad = _canary_payload()
        bad["account_sha256"] = "b" * 64
        with database.transaction():
            AuditLedger(database).append(
                audit_event_id="canary-plan:bad-account",
                category="CANARY_PLAN_EVIDENCE",
                actor="execution-evidence",
                payload=bad,
                created_at=NOW,
            )
        with pytest.raises(RuntimeEvidenceError, match="account identity changed"):
            evidence_runtime.finalize_canary_observation(
                database=database,
                eod_snapshot=_snapshot(),
                session=SESSION,
                created_at=NOW,
                operational_identity=_canary_operational_identity(),
            )
    finally:
        database.close()


def test_load_canary_plan_invalid_payloads_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database.open(tmp_path / "ledger.sqlite3")
    try:
        monkeypatch.setattr(Database, "query_all", lambda self, sql, parameters=(): ({"payload_json": "{"},))
        with pytest.raises(RuntimeEvidenceError, match="invalid JSON"):
            evidence_runtime._load_canary_plan(database, session=SESSION)

        monkeypatch.setattr(Database, "query_all", lambda self, sql, parameters=(): ({"payload_json": "[]"},))
        with pytest.raises(RuntimeEvidenceError, match="not an object"):
            evidence_runtime._load_canary_plan(database, session=SESSION)

        other = _canary_payload()
        other["execution_session"] = (SESSION - timedelta(days=1)).isoformat()
        monkeypatch.setattr(
            Database,
            "query_all",
            lambda self, sql, parameters=(): ({"payload_json": json.dumps(other)},),
        )
        assert evidence_runtime._load_canary_plan(database, session=SESSION) is None
    finally:
        database.close()
