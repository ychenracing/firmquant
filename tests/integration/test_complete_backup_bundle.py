from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from uquant.account import economic_state_sha256, save_account
from uquant.types import AccountState

from firmquant.config import load_settings
from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.backup import (
    BackupBundleInputs,
    BackupError,
    backup_state,
    verify_backup,
)
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


def _complete_inputs(tmp_path: Path, *, account_sha256: str, decision_id: str) -> BackupBundleInputs:
    config = tmp_path / "production.toml"
    config.write_text("# reviewed production configuration\n", encoding="utf-8")
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
    return BackupBundleInputs(
        settings=load_settings(config),
        config_path=config,
        config_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
        safety_manifest_path=safety,
        calendar_manifest_path=calendar,
        active_data_manifest_path=active,
        strategy_data_manifest_path=strategy_data,
        firmquant_commit="e" * 40,
        uquant_commit="f" * 40,
        account_sha256=account_sha256,
        decision_id=decision_id,
        strategy_session=STRATEGY_SESSION,
    )


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
    account_state = AccountState.empty(1000.0)
    account_state.data_hash = "c" * 64
    account_state.data_hash_as_of = STRATEGY_SESSION.isoformat()
    account_state.data_hash_symbols = ["sz300308"]
    account_state.code_hash = "d" * 64
    save_account(account_state, account)
    account_file_sha = hashlib.sha256(account.read_bytes()).hexdigest()
    account_economic_sha = economic_state_sha256(account_state)
    complete_inputs = _complete_inputs(
        tmp_path,
        account_sha256=account_economic_sha,
        decision_id=decision.decision_id,
    )
    expected_config = complete_inputs.config_path.read_bytes()
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    try:
        receipt = backup_state(
            database,
            backup_root,
            account_state_path=account,
            created_at=datetime(2026, 8, 25, 9, tzinfo=UTC),
            complete_inputs=complete_inputs,
        )
    finally:
        database.close()

    verification = verify_backup(receipt.bundle_path)
    assert verification.complete_bundle is True
    assert verification.decision_id == decision.decision_id
    assert verification.account_state_sha256 == account_file_sha
    assert (receipt.bundle_path / "production_config.toml").read_bytes() == expected_config
    deployment = json.loads((receipt.bundle_path / "deployment_record.json").read_text(encoding="utf-8"))
    assert deployment["config_sha256"] == hashlib.sha256(expected_config).hexdigest()
    assert {path.name for path in receipt.bundle_path.iterdir()} == {
        "firmquant.sqlite3",
        "account_state.json",
        "production_config.toml",
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
        if path.suffix in {".json", ".toml"}
    )
    assert b"ARM_MAC_KEY" not in text_members
    assert b"WEBHOOK_TOKEN" not in text_members
    assert b"secret-value" not in text_members


def test_complete_backup_rejects_config_hash_that_does_not_match_actual_file(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    decision = decision_snapshot()
    with database.transaction():
        DecisionSnapshotRepository(database).append(decision)
    account = tmp_path / "account.json"
    account_state = AccountState.empty(1000.0)
    save_account(account_state, account)
    inputs = _complete_inputs(
        tmp_path,
        account_sha256=economic_state_sha256(account_state),
        decision_id=decision.decision_id,
    )
    object.__setattr__(inputs, "config_sha256", "0" * 64)
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    try:
        with pytest.raises(BackupError, match="config identity"):
            backup_state(
                database,
                backup_root,
                account_state_path=account,
                created_at=datetime(2026, 8, 25, 9, tzinfo=UTC),
                complete_inputs=inputs,
            )
    finally:
        database.close()
