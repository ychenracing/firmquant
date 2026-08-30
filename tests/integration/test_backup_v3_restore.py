from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from uquant.account import economic_state_sha256, save_account
from uquant.types import AccountState

from firmquant.application.production_identity import (
    DeploymentIdentity,
    OperationalEvidenceIdentity,
    deployment_caps_sha256,
    semantic_config_sha256,
)
from firmquant.config import Mode, load_settings
from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.backup import (
    BackupBundleInputs,
    BackupReason,
    BackupVerificationError,
    backup_state,
    restore_backup,
    verify_backup,
)
from firmquant.persistence.database import Database
from firmquant.persistence.repositories import DecisionSnapshotRepository, canonical_json
from firmquant.risk.production_policy import ProductionSafetyPolicy
from tests.fixtures.session_cases import STRATEGY_SESSION, decision_snapshot

NOW = datetime(2026, 8, 25, 9, tzinfo=UTC)


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True), encoding="utf-8")


def _safety_payload() -> dict[str, object]:
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


def _v3_case(
    tmp_path: Path,
    *,
    reason: BackupReason = BackupReason.SESSION_CLOSE,
    epoch_account_state_sha256: str | None = None,
    snapshot_session: str | None = None,
):
    config = tmp_path / "production.toml"
    config.write_text("# reviewed production configuration\n", encoding="utf-8")
    settings = load_settings(config)
    safety = tmp_path / "safety.json"
    calendar = tmp_path / "calendar.json"
    active = tmp_path / "active.json"
    strategy = tmp_path / "strategy.json"
    _write_json(safety, _safety_payload())
    _write_json(
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
    generation_members = {"sz300308.csv": "c" * 64}
    generation_id = "gen-" + "c" * 24
    _write_json(
        active,
        {
            "schema": "firmquant.data-generation.v1",
            "generation_id": generation_id,
            "source": "reviewed-test",
            "created_at": NOW.isoformat(),
            "members": generation_members,
            "data_sha256": hashlib.sha256(canonical_json(generation_members).encode()).hexdigest(),
        },
    )
    _write_json(
        strategy,
        {
            "schema": "firmquant.daily-data-manifest.v2",
            "target_session": STRATEGY_SESSION.isoformat(),
            "source": "xtquant",
            "uquant_manifest_sha256": "7" * 64,
            "data_generation_id": generation_id,
            "observations": [
                {
                    "symbol": "sz300308",
                    "latest_observed_session": STRATEGY_SESSION.isoformat(),
                    "suspension_evidence_sha256": None,
                }
            ],
        },
    )

    account_path = tmp_path / "account.json"
    account = AccountState.empty(1000.0)
    account.data_hash = "c" * 64
    account.data_hash_as_of = STRATEGY_SESSION.isoformat()
    account.data_hash_symbols = ["sz300308"]
    account.code_hash = "d" * 64
    save_account(account, account_path)
    account_state_sha256 = economic_state_sha256(account)
    account_id_hash = "0" * 64
    config_sha256 = hashlib.sha256(config.read_bytes()).hexdigest()
    safety_sha256 = hashlib.sha256(safety.read_bytes()).hexdigest()
    deployment = DeploymentIdentity(
        firmquant_commit="e" * 40,
        uquant_commit="f" * 40,
        uquant_tree="1" * 40,
        uquant_package_manifest_sha256="2" * 64,
        uquant_code_fingerprint="3" * 64,
        uquant_config_fingerprint="4" * 64,
        semantic_config_sha256=semantic_config_sha256(settings),
        raw_config_sha256=config_sha256,
        xtquant_safety_manifest_sha256=safety_sha256,
        account_id_hash=account_id_hash,
        account_authority_epoch=1,
        mode_epoch=2,
        mode=Mode.PAPER,
        caps_sha256=deployment_caps_sha256(settings),
        production_policy_sha256=ProductionSafetyPolicy.from_settings(settings).sha256,
    )
    reason_phase = {
        BackupReason.SESSION_CLOSE: "EOD",
        BackupReason.MODE_TRANSITION: "MODE_TRANSITION",
        BackupReason.ACCOUNT_REBASELINE: "ACCOUNT_REBASELINE",
    }[reason]
    reason_decision_id = decision_snapshot().decision_id if reason is BackupReason.SESSION_CLOSE else None
    evidence = OperationalEvidenceIdentity(
        deployment_identity=deployment,
        account_state_sha256=account_state_sha256,
        broker_snapshot_id="snapshot-v3",
        broker_snapshot_sha256="9" * 64,
        broker_event_watermark=7,
        snapshot_started_at=datetime(2026, 8, 25, 8, 59, 59, tzinfo=UTC),
        snapshot_completed_at=NOW,
        snapshot_duration_ms=23,
        calendar_sha256=hashlib.sha256(calendar.read_bytes()).hexdigest(),
        active_data_generation_sha256=hashlib.sha256(active.read_bytes()).hexdigest(),
        strategy_data_manifest_sha256=hashlib.sha256(strategy.read_bytes()).hexdigest(),
        strategy_session=STRATEGY_SESSION,
        decision_id=reason_decision_id,
        phase=reason_phase,
        kind="BACKUP",
    )

    account_epoch_state = epoch_account_state_sha256 or account_state_sha256
    account_epoch_payload = canonical_json(
        {
            "schema": "firmquant.account-authority-epoch.v1",
            "epoch": 1,
            "account_id_hash": account_id_hash,
            "account_state_sha256": account_epoch_state,
            "deployment_identity_sha256": deployment.sha256,
            "created_at": NOW,
        }
    )
    mode_epoch_payload = canonical_json(
        {
            "schema": "firmquant.mode-epoch.v1",
            "epoch": 2,
            "mode": Mode.PAPER,
            "deployment_identity_sha256": deployment.sha256,
            "caps_sha256": deployment.caps_sha256,
            "created_at": NOW,
        }
    )

    database = Database.open(tmp_path / "firmquant.sqlite3")
    with database.transaction():
        database.write(
            """
            INSERT INTO runtime_state(singleton_id,mode,state,revision,reason,blockers_json,updated_at)
            VALUES(1,'PAPER','READY',1,'test','[]',?)
            """,
            (NOW.isoformat(),),
        )
        database.write(
            """
            INSERT INTO account_authority_epochs(
                epoch,account_id_hash,account_state_sha256,deployment_identity_sha256,
                source_binding_id,payload_json,payload_sha256,created_at
            ) VALUES(1,?,?,?,?,?,?,?)
            """,
            (
                account_id_hash,
                account_epoch_state,
                deployment.sha256,
                None,
                account_epoch_payload,
                hashlib.sha256(account_epoch_payload.encode("utf-8")).hexdigest(),
                NOW.isoformat(),
            ),
        )
        database.write("INSERT INTO account_authority_active(singleton_id,epoch) VALUES(1,1)")
        database.write(
            """
            INSERT INTO mode_epochs(
                epoch,mode,deployment_identity_sha256,caps_sha256,
                payload_json,payload_sha256,created_at
            ) VALUES(2,'PAPER',?,?,?,?,?)
            """,
            (
                deployment.sha256,
                deployment.caps_sha256,
                mode_epoch_payload,
                hashlib.sha256(mode_epoch_payload.encode("utf-8")).hexdigest(),
                NOW.isoformat(),
            ),
        )
        database.write("UPDATE mode_epoch_active SET epoch=2 WHERE singleton_id=1")
        database.write(
            """
            INSERT INTO deployment_identities(
                deployment_identity_sha256,account_id_hash,account_authority_epoch,
                mode_epoch,mode,payload_json,payload_sha256,created_at
            ) VALUES(?,?,1,2,'PAPER',?,?,?)
            """,
            (
                deployment.sha256,
                account_id_hash,
                deployment.canonical_json,
                deployment.sha256,
                NOW.isoformat(),
            ),
        )
        database.write(
            """
            INSERT INTO broker_snapshots(
                snapshot_id,account_id_hash,account_type,session_date,captured_at,
                broker_event_watermark,raw_payload_sha256,complete,
                started_at,completed_at,duration_ms
            ) VALUES(?,?,'CASH',?,?,?,?,1,?,?,?)
            """,
            (
                evidence.broker_snapshot_id,
                account_id_hash,
                snapshot_session or STRATEGY_SESSION.isoformat(),
                evidence.snapshot_completed_at.isoformat(),
                evidence.broker_event_watermark,
                evidence.broker_snapshot_sha256,
                evidence.snapshot_started_at.isoformat(),
                evidence.snapshot_completed_at.isoformat(),
                evidence.snapshot_duration_ms,
            ),
        )
        database.write(
            "INSERT INTO cash_snapshots(snapshot_id,available_cash,total_assets) VALUES(?,?,?)",
            (evidence.broker_snapshot_id, "1000", "1000"),
        )
        database.write(
            """
            INSERT INTO operational_evidence_receipts(
                receipt_id,operational_evidence_identity_sha256,deployment_identity_sha256,
                account_authority_epoch,mode_epoch,account_state_sha256,broker_snapshot_id,
                strategy_session,phase,kind,payload_json,payload_sha256,created_at
            ) VALUES(?,?,?,1,2,?,?,?,?,?,?,?,?)
            """,
            (
                "evidence-v3",
                evidence.sha256,
                deployment.sha256,
                account_state_sha256,
                evidence.broker_snapshot_id,
                STRATEGY_SESSION.isoformat(),
                evidence.phase,
                evidence.kind,
                evidence.canonical_json,
                evidence.sha256,
                NOW.isoformat(),
            ),
        )
        DecisionSnapshotRepository(database).append(decision_snapshot())
        AuditLedger(database).append(
            audit_event_id="audit-before-backup",
            category="RUNTIME",
            actor="test",
            payload={"state": "READY"},
            created_at=NOW,
        )
    inputs = BackupBundleInputs(
        settings=settings,
        config_path=config,
        config_sha256=config_sha256,
        safety_manifest_path=safety,
        calendar_manifest_path=calendar,
        active_data_manifest_path=active,
        strategy_data_manifest_path=strategy,
        firmquant_commit=deployment.firmquant_commit,
        uquant_commit=deployment.uquant_commit,
        account_sha256=account_state_sha256,
        decision_id=reason_decision_id,
        strategy_session=STRATEGY_SESSION,
        reason=reason,
        deployment_identity=deployment,
        operational_evidence_identity=evidence,
    )
    root = tmp_path / "backups"
    root.mkdir()
    return database, account_path, inputs, root


@pytest.mark.parametrize("reason", list(BackupReason))
def test_v3_bundle_cross_binds_reason_epochs_and_identities(tmp_path: Path, reason: BackupReason) -> None:
    database, account, inputs, root = _v3_case(tmp_path, reason=reason)
    try:
        receipt = backup_state(
            database,
            root,
            account_state_path=account,
            complete_inputs=inputs,
            created_at=NOW,
        )
        verified = verify_backup(receipt.bundle_path)
        operation = database.query_one(
            "SELECT stage,bundle_name FROM backup_publication_operations WHERE backup_id=?",
            (receipt.backup_id,),
        )
    finally:
        database.close()
    assert verified.schema_version == 3
    assert verified.operational_schema_version == 6
    assert verified.reason is reason
    assert verified.deployment_identity_sha256 == inputs.deployment_identity.sha256
    assert verified.operational_evidence_identity_sha256 == inputs.operational_evidence_identity.sha256
    assert verified.production_authority is True
    assert {path.name for path in receipt.bundle_path.iterdir()} == {
        "firmquant.sqlite3",
        "account_state.json",
        "production_config.toml",
        "xtquant_safety_manifest.json",
        "trading_calendar.json",
        "active_data_source.json",
        "strategy_data_manifest.json",
        "deployment_identity.json",
        "operational_evidence_identity.json",
        "account_authority_epoch.json",
        "mode_epoch.json",
        "manifest.json",
    }
    assert operation is not None
    assert tuple(operation) == ("RECEIPT_COMMITTED", receipt.bundle_path.name)


def test_generic_backup_is_explicit_legacy_non_authoritative(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    root = tmp_path / "backups"
    root.mkdir()
    try:
        receipt = backup_state(database, root, created_at=NOW)
    finally:
        database.close()
    verified = verify_backup(receipt.bundle_path)
    assert verified.schema_version == 1
    assert verified.operational_schema_version == 6
    assert verified.complete_bundle is False
    assert verified.production_authority is False


def test_session_close_v3_accepts_current_state_after_stable_epoch_baseline(
    tmp_path: Path,
) -> None:
    baseline_sha256 = "a" * 64
    database, account, inputs, root = _v3_case(
        tmp_path,
        epoch_account_state_sha256=baseline_sha256,
    )
    assert baseline_sha256 != inputs.account_sha256
    try:
        receipt = backup_state(
            database,
            root,
            account_state_path=account,
            complete_inputs=inputs,
            created_at=NOW,
        )
    finally:
        database.close()
    assert verify_backup(receipt.bundle_path).production_authority is True


def test_restore_forces_disarmed_revokes_authority_and_is_idempotent(tmp_path: Path) -> None:
    database, account, inputs, root = _v3_case(tmp_path)
    try:
        with database.transaction():
            database.write(
                """
                INSERT INTO arm_leases(
                    lease_id,mode,host_hash,account_hash,firmquant_commit,uquant_commit,
                    config_sha256,identity_payload_sha256,issued_at,expires_at,lease_mac
                ) VALUES('lease-1','LIVE',?,?,?,?,?,?,?,?,?)
                """,
                (
                    "7" * 64,
                    "8" * 64,
                    "e" * 40,
                    "f" * 40,
                    "9" * 64,
                    "a" * 64,
                    NOW.isoformat(),
                    datetime(2026, 8, 25, 9, 10, tzinfo=UTC).isoformat(),
                    "b" * 64,
                ),
            )
            database.write(
                """
                INSERT INTO writer_leases(
                    singleton_id,owner_id,host_hash,process_id,acquired_at,renewed_at,expires_at,generation
                ) VALUES(1,'stale',?,123,?,?,?,1)
                """,
                (
                    "c" * 64,
                    NOW.isoformat(),
                    NOW.isoformat(),
                    datetime(2026, 8, 25, 9, 10, tzinfo=UTC).isoformat(),
                ),
            )
        backup = backup_state(
            database,
            root,
            account_state_path=account,
            complete_inputs=inputs,
            created_at=NOW,
        )
    finally:
        database.close()

    destination = tmp_path / "restored"
    first = restore_backup(backup.bundle_path, destination, restored_at=NOW)
    second = restore_backup(backup.bundle_path, destination, restored_at=NOW)
    restored = Database.open(destination / "firmquant.sqlite3")
    try:
        assert restored.scalar("SELECT state FROM runtime_state WHERE singleton_id=1") == "DISARMED"
        assert restored.scalar("SELECT count(*) FROM arm_leases WHERE revoked_at IS NULL") == 0
        assert restored.scalar("SELECT count(*) FROM writer_leases") == 0
        assert restored.scalar("SELECT count(*) FROM production_heartbeat") == 0
        assert restored.scalar("SELECT count(*) FROM restore_receipts") == 1
        AuditLedger(restored).verify()
    finally:
        restored.close()
    assert first == second
    assert first.requires_fresh_snapshot is True
    assert first.requires_reconciliation is True
    assert first.destination == destination


def test_restore_rejects_corruption_nonempty_destination_and_symlink(tmp_path: Path) -> None:
    database, account, inputs, root = _v3_case(tmp_path)
    try:
        backup = backup_state(
            database,
            root,
            account_state_path=account,
            complete_inputs=inputs,
            created_at=NOW,
        )
    finally:
        database.close()

    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    marker = nonempty / "incident.txt"
    marker.write_text("preserve", encoding="utf-8")
    with pytest.raises(BackupVerificationError, match="destination"):
        restore_backup(backup.bundle_path, nonempty, restored_at=NOW)
    assert marker.read_text(encoding="utf-8") == "preserve"

    symlink = tmp_path / "symlink"
    symlink.symlink_to(nonempty, target_is_directory=True)
    with pytest.raises(BackupVerificationError, match=r"symlink|reparse"):
        restore_backup(backup.bundle_path, symlink, restored_at=NOW)

    (backup.bundle_path / "deployment_identity.json").write_text("{}", encoding="utf-8")
    untouched = tmp_path / "untouched"
    with pytest.raises(BackupVerificationError, match="SHA-256"):
        restore_backup(backup.bundle_path, untouched, restored_at=NOW)
    assert not untouched.exists()


def test_v3_verification_rejects_cross_identity_drift(tmp_path: Path) -> None:
    database, account, inputs, root = _v3_case(tmp_path)
    try:
        receipt = backup_state(
            database,
            root,
            account_state_path=account,
            complete_inputs=inputs,
            created_at=NOW,
        )
    finally:
        database.close()
    manifest_path = receipt.bundle_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["account_authority_epoch"] = 2
    manifest_path.write_text(
        json.dumps(manifest, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    with pytest.raises(BackupVerificationError, match=r"authority|identity"):
        verify_backup(receipt.bundle_path)


def test_v3_verification_rejects_noncanonical_manifest_json(tmp_path: Path) -> None:
    database, account, inputs, root = _v3_case(tmp_path)
    try:
        receipt = backup_state(
            database,
            root,
            account_state_path=account,
            complete_inputs=inputs,
            created_at=NOW,
        )
    finally:
        database.close()
    manifest_path = receipt.bundle_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    with pytest.raises(BackupVerificationError, match="canonical"):
        verify_backup(receipt.bundle_path)


def test_restore_rejects_destination_nested_inside_source_bundle(tmp_path: Path) -> None:
    database, account, inputs, root = _v3_case(tmp_path)
    try:
        source = backup_state(
            database,
            root,
            account_state_path=account,
            complete_inputs=inputs,
            created_at=NOW,
        )
    finally:
        database.close()
    members_before = {path.name for path in source.bundle_path.iterdir()}
    with pytest.raises(BackupVerificationError, match=r"source|destination"):
        restore_backup(source.bundle_path, source.bundle_path / "restored", restored_at=NOW)
    assert {path.name for path in source.bundle_path.iterdir()} == members_before


def test_v3_backup_recovers_exact_bundle_after_rename_before_sql_progression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import firmquant.persistence.backup as backup_module

    database, account, inputs, root = _v3_case(tmp_path)
    real_fsync_directory = backup_module._fsync_directory
    failed = False

    def fail_first_parent_fsync(path: Path) -> None:
        nonlocal failed
        if path == root and not failed:
            failed = True
            raise OSError("injected parent fsync failure")
        real_fsync_directory(path)

    monkeypatch.setattr(backup_module, "_fsync_directory", fail_first_parent_fsync)
    try:
        with pytest.raises(backup_module.BackupError, match="publish"):
            backup_state(
                database,
                root,
                account_state_path=account,
                complete_inputs=inputs,
                created_at=NOW,
            )
        row = database.query_one("SELECT stage FROM backup_publication_operations")
        assert row is not None and row["stage"] == "PREPARED"
        monkeypatch.setattr(backup_module, "_fsync_directory", real_fsync_directory)
        receipt = backup_state(
            database,
            root,
            account_state_path=account,
            complete_inputs=inputs,
            created_at=datetime(2026, 8, 25, 9, 5, tzinfo=UTC),
        )
        assert (
            database.scalar(
                "SELECT count(*) FROM backup_publication_operations WHERE stage='RECEIPT_COMMITTED'"
            )
            == 1
        )
        assert database.scalar("SELECT count(*) FROM backup_receipts WHERE bundle_schema_version=3") == 1
    finally:
        database.close()
    assert receipt.bundle_path.is_dir()
    assert [path for path in root.iterdir() if not path.name.startswith(".")] == [receipt.bundle_path]


def test_restore_recovers_published_directory_before_receipt_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import firmquant.persistence.backup as backup_module

    database, account, inputs, root = _v3_case(tmp_path)
    try:
        source = backup_state(
            database,
            root,
            account_state_path=account,
            complete_inputs=inputs,
            created_at=NOW,
        )
    finally:
        database.close()
    destination = tmp_path / "restored-after-crash"
    real_finalize = backup_module._finalize_restore
    calls = 0

    def fail_once(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected crash after directory publication")
        return real_finalize(*args, **kwargs)

    monkeypatch.setattr(backup_module, "_finalize_restore", fail_once)
    with pytest.raises(OSError, match="injected crash"):
        restore_backup(source.bundle_path, destination, restored_at=NOW)
    assert destination.is_dir()
    monkeypatch.setattr(backup_module, "_finalize_restore", real_finalize)
    receipt = restore_backup(source.bundle_path, destination, restored_at=NOW)
    assert receipt.destination == destination
    restored = Database.open(destination / "firmquant.sqlite3")
    try:
        assert restored.scalar("SELECT count(*) FROM restore_operations WHERE stage='RECEIPT_COMMITTED'") == 1
        assert restored.scalar("SELECT count(*) FROM restore_receipts") == 1
    finally:
        restored.close()


def test_restore_recovers_exact_staging_directory_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import firmquant.persistence.backup as backup_module

    database, account, inputs, root = _v3_case(tmp_path)
    try:
        source = backup_state(
            database,
            root,
            account_state_path=account,
            complete_inputs=inputs,
            created_at=NOW,
        )
    finally:
        database.close()
    destination = tmp_path / "restored-from-stage"
    real_fsync_directory = backup_module._fsync_directory
    failed = False

    def fail_staging_fsync(path: Path) -> None:
        nonlocal failed
        if path.name.endswith(".staging") and not failed:
            failed = True
            raise OSError("injected staging fsync failure")
        real_fsync_directory(path)

    monkeypatch.setattr(backup_module, "_fsync_directory", fail_staging_fsync)
    with pytest.raises(backup_module.BackupError, match="publish"):
        restore_backup(source.bundle_path, destination, restored_at=NOW)
    staging = next(tmp_path.glob(".restored-from-stage.*.staging"))
    assert staging.is_dir()

    monkeypatch.setattr(backup_module, "_fsync_directory", real_fsync_directory)
    receipt = restore_backup(source.bundle_path, destination, restored_at=NOW)
    assert receipt.destination == destination
    assert not staging.exists()
