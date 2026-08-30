from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import firmquant.persistence.backup as backup
from firmquant.persistence.database import PersistenceError

NOW = datetime(2026, 8, 25, 8, tzinfo=UTC)
ZERO = "0" * 64


def _verification(**changes: object) -> backup.BackupVerification:
    verification = backup.BackupVerification(
        backup_id="backup-restore-contract",
        database_sha256="1" * 64,
        account_state_sha256="2" * 64,
        manifest_sha256="3" * 64,
        audit_count=5,
        audit_head_hash="4" * 64,
        schema_version=3,
        operational_schema_version=backup.CURRENT_SCHEMA_VERSION,
        complete_bundle=True,
        production_authority=True,
        decision_id="decision-restore-contract",
        reason=backup.BackupReason.SESSION_CLOSE,
        deployment_identity_sha256="5" * 64,
        operational_evidence_identity_sha256="6" * 64,
        account_authority_epoch=7,
        mode_epoch=8,
        broker_snapshot_id="snapshot-restore-contract",
        broker_snapshot_sha256="9" * 64,
    )
    return replace(verification, **changes)


class _SanitizedDatabase:
    def __init__(
        self,
        *,
        foreign_keys: list[object] | None = None,
        runtime_state: object = "DISARMED",
        active_arms: object = 0,
        writers: object = 0,
        heartbeats: object = 0,
        operation: object = object(),
    ) -> None:
        self.foreign_keys = [] if foreign_keys is None else foreign_keys
        self.runtime_state = runtime_state
        self.active_arms = active_arms
        self.writers = writers
        self.heartbeats = heartbeats
        self.operation = operation
        self.integrity_checked = False

    def integrity_check(self) -> None:
        self.integrity_checked = True

    def query_all(self, sql: str) -> list[object]:
        assert sql == "PRAGMA foreign_key_check"
        return self.foreign_keys

    def scalar(self, sql: str) -> object:
        if "FROM runtime_state" in sql:
            return self.runtime_state
        if "FROM arm_leases" in sql:
            return self.active_arms
        if "FROM writer_leases" in sql:
            return self.writers
        if "FROM production_heartbeat" in sql:
            return self.heartbeats
        raise AssertionError(f"unexpected scalar query: {sql}")

    def query_one(self, sql: str, parameters: tuple[object, ...]) -> object | None:
        assert "FROM restore_operations" in sql
        assert parameters == ("restore-contract",)
        return self.operation


@pytest.mark.parametrize(
    ("changes", "logical_sha256", "message"),
    (
        ({"foreign_keys": [object()]}, "a" * 64, "foreign keys are invalid"),
        ({"runtime_state": "ARMED"}, "a" * 64, "runtime is not DISARMED"),
        ({"active_arms": 1}, "a" * 64, "retained active arm authority"),
        ({"writers": 1}, "a" * 64, "retained writer authority"),
        ({"heartbeats": 1}, "a" * 64, "retained heartbeat authority"),
        ({}, "b" * 64, "sanitized logical state digest changed"),
        ({"operation": None}, "a" * 64, "operation evidence is missing"),
    ),
)
def test_verify_sanitized_restore_rejects_unsafe_or_unproven_state(
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, Any],
    logical_sha256: str,
    message: str,
) -> None:
    database = _SanitizedDatabase(**changes)
    monkeypatch.setattr(backup, "_logical_state_sha256", lambda _database: logical_sha256)
    monkeypatch.setattr(
        backup,
        "AuditLedger",
        lambda _database: SimpleNamespace(verify=lambda: SimpleNamespace(count=12, head_hash="c" * 64)),
    )

    with pytest.raises(backup.BackupVerificationError, match=message):
        backup._verify_sanitized_restore(
            database,  # type: ignore[arg-type]
            restore_id="restore-contract",
            expected_sha256="a" * 64,
        )

    assert database.integrity_checked is True


def test_verify_sanitized_restore_returns_verified_audit_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _SanitizedDatabase()
    monkeypatch.setattr(backup, "_logical_state_sha256", lambda _database: "a" * 64)
    monkeypatch.setattr(
        backup,
        "AuditLedger",
        lambda _database: SimpleNamespace(verify=lambda: SimpleNamespace(count=12, head_hash="c" * 64)),
    )

    assert backup._verify_sanitized_restore(
        database,  # type: ignore[arg-type]
        restore_id="restore-contract",
        expected_sha256="a" * 64,
    ) == (12, "c" * 64)


class _StagingDatabase:
    def __init__(self, operation: dict[str, object]) -> None:
        self.operation = operation
        self.closed = False

    def query_one(self, sql: str, parameters: tuple[object, ...]) -> dict[str, object]:
        assert "FROM restore_operations" in sql
        assert parameters == ("restore-contract",)
        return self.operation

    def close(self) -> None:
        self.closed = True


def _operation(
    verification: backup.BackupVerification,
    *,
    stage: str = "PREPARED",
    sanitized_state_sha256: str | None = None,
) -> dict[str, object]:
    assert verification.reason is not None
    assert verification.deployment_identity_sha256 is not None
    assert verification.operational_evidence_identity_sha256 is not None
    assert verification.account_authority_epoch is not None
    assert verification.mode_epoch is not None
    return {
        "stage": stage,
        "backup_id": verification.backup_id,
        "source_reason": verification.reason.value,
        "source_manifest_sha256": verification.manifest_sha256,
        "source_database_sha256": verification.database_sha256,
        "destination_identity_sha256": "d" * 64,
        "sanitized_state_sha256": sanitized_state_sha256,
        "deployment_identity_sha256": verification.deployment_identity_sha256,
        "operational_evidence_identity_sha256": (verification.operational_evidence_identity_sha256),
        "account_authority_epoch": verification.account_authority_epoch,
        "mode_epoch": verification.mode_epoch,
    }


def _patch_staging_database(
    monkeypatch: pytest.MonkeyPatch,
    database: _StagingDatabase,
    *,
    schema_version: int = backup.CURRENT_SCHEMA_VERSION,
) -> None:
    monkeypatch.setattr(backup, "_verify_restored_static_members", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        backup.Database,
        "open_read_only",
        lambda *_args, **_kwargs: database,
    )
    monkeypatch.setattr(backup, "_database_schema_version", lambda _database: schema_version)
    monkeypatch.setattr(backup, "_verify_migration_prefix", lambda *_args, **_kwargs: None)


@pytest.mark.parametrize(
    ("case", "cause_message"),
    (
        ("schema", "staging schema is not current"),
        ("proof", "staging operation proof conflicts"),
        ("stage", "staging operation proof conflicts"),
        ("staged_null", "STAGED proof lacks logical state"),
        ("prepared_digest", "PREPARED proof has premature logical state"),
    ),
)
def test_inspect_staging_operation_rejects_nonresumable_proof(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case: str,
    cause_message: str,
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "firmquant.sqlite3").touch()
    verification = _verification()
    operation = _operation(verification)
    schema_version = backup.CURRENT_SCHEMA_VERSION
    if case == "schema":
        schema_version -= 1
    elif case == "proof":
        operation["source_manifest_sha256"] = ZERO
    elif case == "stage":
        operation["stage"] = "PUBLISHED"
    elif case == "staged_null":
        operation["stage"] = "STAGED"
    elif case == "prepared_digest":
        operation["sanitized_state_sha256"] = "a" * 64
    database = _StagingDatabase(operation)
    _patch_staging_database(monkeypatch, database, schema_version=schema_version)

    with pytest.raises(
        backup.BackupVerificationError,
        match="staging collision requires manual preservation",
    ) as failure:
        backup._inspect_staging_operation(
            staging,
            verification=verification,
            restore_id="restore-contract",
            destination_sha256="d" * 64,
        )

    assert failure.value.__cause__ is not None
    assert cause_message in str(failure.value.__cause__)
    assert database.closed is True


def test_inspect_staging_operation_verifies_staged_logical_proof(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "firmquant.sqlite3").touch()
    verification = _verification()
    database = _StagingDatabase(_operation(verification, stage="STAGED", sanitized_state_sha256="a" * 64))
    _patch_staging_database(monkeypatch, database)
    observed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        backup,
        "_verify_sanitized_restore",
        lambda _database, *, restore_id, expected_sha256: observed.append((restore_id, expected_sha256)),
    )

    assert backup._inspect_staging_operation(
        staging,
        verification=verification,
        restore_id="restore-contract",
        destination_sha256="d" * 64,
    ) == ("STAGED", "a" * 64)
    assert observed == [("restore-contract", "a" * 64)]
    assert database.closed is True


def test_inspect_staging_operation_wraps_persistence_failure_as_preservation_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "firmquant.sqlite3").touch()
    monkeypatch.setattr(backup, "_verify_restored_static_members", lambda *_args, **_kwargs: None)

    def fail_open(*_args: object, **_kwargs: object) -> None:
        raise PersistenceError("probe failed")

    monkeypatch.setattr(backup.Database, "open_read_only", fail_open)

    with pytest.raises(
        backup.BackupVerificationError,
        match="staging collision requires manual preservation",
    ) as failure:
        backup._inspect_staging_operation(
            staging,
            verification=_verification(),
            restore_id="restore-contract",
            destination_sha256="d" * 64,
        )

    assert isinstance(failure.value.__cause__, PersistenceError)


@pytest.mark.parametrize(
    "verification",
    (
        _verification(schema_version=2),
        _verification(production_authority=False),
    ),
)
def test_restore_backup_accepts_only_authoritative_schema_v3(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    verification: backup.BackupVerification,
) -> None:
    bundle = tmp_path / "bundle"
    destination = tmp_path / "restored"
    monkeypatch.setattr(
        backup,
        "_validate_restore_paths",
        lambda *_args: (bundle, destination, object()),
    )
    monkeypatch.setattr(backup, "verify_backup", lambda _bundle: verification)

    with pytest.raises(
        backup.BackupVerificationError,
        match="only verified schema-v3 backups",
    ):
        backup.restore_backup(bundle, destination, restored_at=NOW)


def test_restore_backup_rejects_naive_timestamp_before_deriving_restore_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    destination = tmp_path / "restored"
    monkeypatch.setattr(
        backup,
        "_validate_restore_paths",
        lambda *_args: (bundle, destination, object()),
    )
    monkeypatch.setattr(backup, "verify_backup", lambda _bundle: _verification())

    with pytest.raises(backup.BackupVerificationError, match="timestamp must be timezone-aware"):
        backup.restore_backup(
            bundle,
            destination,
            restored_at=datetime(2026, 8, 25, 8),
        )
