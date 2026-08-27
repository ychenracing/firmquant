from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from firmquant.config import Settings
from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.backup import BackupBundleInputs, backup_state, verify_backup
from firmquant.persistence.database import Database
from firmquant.persistence.repositories import DecisionSnapshotRepository
from tests.fixtures.session_cases import STRATEGY_SESSION, decision_snapshot


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True), encoding="utf-8")


def safety_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_name": "reviewed-test",
        "source_sha256": "a" * 64,
        "probe_symbol": "sz300308",
        "equity_product_types": [1],
        "trading_instrument_statuses": [1],
        "open_stock_statuses": [1],
        "auction_stock_statuses": [2],
        "break_stock_statuses": [3],
        "closed_stock_statuses": [4],
        "trading_units": {"SH": 100, "SZ": 100, "BJ": 100},
        "volume_multipliers": {"SH": 1, "SZ": 1, "BJ": 1},
        "commission_rate": "0.0003",
        "minimum_commission": "5",
        "stamp_duty_rate": "0.0005",
        "transfer_fee_rate": "0.00001",
    }


def test_complete_backup_restores_deployment_identity_and_contains_no_secret_material(
    tmp_path: Path,
) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    decision = decision_snapshot()
    with database.transaction():
        AuditLedger(database).append(
            audit_event_id="audit-before-backup",
            category="RUNTIME",
            actor="test",
            payload={"state": "READY"},
            created_at=datetime(2026, 8, 25, 8, tzinfo=UTC),
        )
        DecisionSnapshotRepository(database).append(decision)
    account = tmp_path / "account.json"
    account.write_text('{"schema_version":1,"cash":1000}', encoding="utf-8")
    account_sha = hashlib.sha256(account.read_bytes()).hexdigest()
    safety = tmp_path / "safety.json"
    calendar = tmp_path / "calendar.json"
    active = tmp_path / "active.json"
    strategy_data = tmp_path / "strategy-data.json"
    write_json(safety, safety_payload())
    write_json(
        calendar,
        {
            "schema_version": 1,
            "source_name": "reviewed-calendar",
            "source_sha256": "b" * 64,
            "covered_start": "2026-08-01",
            "covered_end": "2026-09-30",
            "sessions": ["2026-08-24", "2026-08-25"],
        },
    )
    write_json(active, {"schema": "firmquant.data-generation.v1", "data_sha256": "c" * 64})
    write_json(
        strategy_data,
        {"schema": "firmquant.daily-data-manifest.v2", "target_session": "2026-08-25"},
    )
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    try:
        receipt = backup_state(
            database,
            backup_root,
            account_state_path=account,
            created_at=datetime(2026, 8, 25, 9, tzinfo=UTC),
            complete_inputs=BackupBundleInputs(
                settings=Settings(),
                config_sha256="d" * 64,
                safety_manifest_path=safety,
                calendar_manifest_path=calendar,
                active_data_manifest_path=active,
                strategy_data_manifest_path=strategy_data,
                firmquant_commit="e" * 40,
                uquant_commit="f" * 40,
                account_sha256=account_sha,
                decision_id=decision.decision_id,
                strategy_session=STRATEGY_SESSION,
            ),
        )
    finally:
        database.close()

    verification = verify_backup(receipt.bundle_path)
    assert verification.complete_bundle is True
    assert verification.decision_id == decision.decision_id
    assert verification.account_state_sha256 == account_sha
    assert {path.name for path in receipt.bundle_path.iterdir()} == {
        "firmquant.sqlite3",
        "account_state.json",
        "production_config.json",
        "xtquant_safety_manifest.json",
        "trading_calendar.json",
        "active_data_source.json",
        "strategy_data_manifest.json",
        "deployment_record.json",
        "manifest.json",
    }
    text_members = b"\n".join(
        path.read_bytes()
        for path in receipt.bundle_path.iterdir()
        if path.suffix == ".json"
    )
    assert b"ARM_MAC_KEY" not in text_members
    assert b"WEBHOOK_TOKEN" not in text_members
    assert b"secret-value" not in text_members
