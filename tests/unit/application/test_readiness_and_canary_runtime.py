from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from firmquant.application import execution_evidence_runtime as evidence_runtime
from firmquant.application import live_readiness_runtime as readiness_runtime
from firmquant.application.execution_evidence import BlockerCode
from firmquant.application.execution_evidence_runtime import RuntimeEvidenceError
from firmquant.config import Settings
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
        assert "ACCOUNT_BINDING" in snapshot.blockers
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
        database.execute(
            "INSERT INTO production_heartbeat(singleton_id,observed_at,control_request_state) VALUES(1,?,?)",
            ((NOW - timedelta(seconds=5)).isoformat(), "IDLE"),
        )
        assert readiness_runtime._heartbeat(database, NOW) == (True, True, 5.0)
        database.execute(
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
        database.execute(
            "INSERT INTO backup_receipts(backup_id,created_at,manifest_json,verification_status) VALUES(?,?,?,?)",
            ("backup-1", NOW.isoformat(), manifest, "VERIFIED"),
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


def test_canary_missing_execution_becomes_unknown_observation(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "ledger.sqlite3")
    try:
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
        evidence_runtime._plan_targets({})
    with pytest.raises(RuntimeEvidenceError):
        evidence_runtime._plan_orders({"orders": ["bad"]})

    database = Database.open(tmp_path / "ledger.sqlite3")
    try:
        assert evidence_runtime._load_canary_plan(database, session=SESSION) is None
        bad = _canary_payload()
        bad["account_sha256"] = "b" * 64
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
            )
    finally:
        database.close()
