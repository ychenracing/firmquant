from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.backup import (
    BackupVerificationError,
    backup_state,
    verify_backup,
)
from firmquant.persistence.database import Database
from firmquant.persistence.schema import CURRENT_SCHEMA_VERSION


def _database_with_audit(path: Path) -> Database:
    database = Database.open(path)
    with database.transaction():
        AuditLedger(database).append(
            audit_event_id="audit-1",
            category="RUNTIME",
            actor="system",
            payload={"state": "READY"},
            created_at=datetime(2026, 8, 25, 1, tzinfo=UTC),
        )
    return database


def test_backup_uses_online_copy_atomic_bundle_and_restore_verification(tmp_path: Path) -> None:
    database = _database_with_audit(tmp_path / "firmquant.sqlite3")
    account_state = tmp_path / "account_state.json"
    account_state.write_text('{"schema_version":1,"cash":100000}', encoding="utf-8")
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    try:
        receipt = backup_state(
            database,
            backup_root,
            account_state_path=account_state,
            created_at=datetime(2026, 8, 25, 2, tzinfo=UTC),
        )
    finally:
        database.close()

    verification = verify_backup(receipt.bundle_path)
    assert verification.database_sha256 == receipt.database_sha256
    assert verification.account_state_sha256 == receipt.account_state_sha256
    assert verification.audit_count == 1
    assert verification.schema_version == 1
    assert verification.operational_schema_version == CURRENT_SCHEMA_VERSION
    assert {path.name for path in receipt.bundle_path.iterdir()} == {
        "account_state.json",
        "firmquant.sqlite3",
        "manifest.json",
    }


def test_corrupt_backup_is_rejected_without_deleting_incident_evidence(tmp_path: Path) -> None:
    database = _database_with_audit(tmp_path / "firmquant.sqlite3")
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    try:
        receipt = backup_state(
            database,
            backup_root,
            created_at=datetime(2026, 8, 25, 2, tzinfo=UTC),
        )
    finally:
        database.close()
    backup_database = receipt.bundle_path / "firmquant.sqlite3"
    backup_database.write_bytes(b"corrupt")

    with pytest.raises(BackupVerificationError, match="database SHA-256"):
        verify_backup(receipt.bundle_path)

    assert receipt.bundle_path.is_dir()
    assert backup_database.read_bytes() == b"corrupt"


def test_legacy_verification_is_byte_and_directory_non_mutating(tmp_path: Path) -> None:
    database = _database_with_audit(tmp_path / "firmquant.sqlite3")
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    try:
        receipt = backup_state(
            database,
            backup_root,
            created_at=datetime(2026, 8, 25, 2, tzinfo=UTC),
        )
    finally:
        database.close()
    before_names = {path.name for path in receipt.bundle_path.iterdir()}
    before_database = (receipt.bundle_path / "firmquant.sqlite3").read_bytes()

    first = verify_backup(receipt.bundle_path)
    second = verify_backup(receipt.bundle_path)

    assert first == second
    assert {path.name for path in receipt.bundle_path.iterdir()} == before_names
    assert (receipt.bundle_path / "firmquant.sqlite3").read_bytes() == before_database
    assert not (receipt.bundle_path / "firmquant.sqlite3-wal").exists()
    assert not (receipt.bundle_path / "firmquant.sqlite3-shm").exists()


def test_backup_manifest_never_contains_source_paths_or_account_contents(tmp_path: Path) -> None:
    database = _database_with_audit(tmp_path / "firmquant.sqlite3")
    account_state = tmp_path / "very-sensitive-account-name.json"
    account_state.write_text('{"account_number":"secret-value"}', encoding="utf-8")
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    try:
        receipt = backup_state(
            database,
            backup_root,
            account_state_path=account_state,
            created_at=datetime(2026, 8, 25, 2, tzinfo=UTC),
        )
    finally:
        database.close()

    manifest = (receipt.bundle_path / "manifest.json").read_text(encoding="utf-8")
    assert "very-sensitive-account-name" not in manifest
    assert "secret-value" not in manifest
    assert str(tmp_path) not in manifest


def test_backup_fsync_uses_windows_compatible_writable_handles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database_with_audit(tmp_path / "firmquant.sqlite3")
    account_state = tmp_path / "account_state.json"
    account_state.write_text('{"schema_version":1}', encoding="utf-8")
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    real_fsync = os.fsync
    verified_descriptors = 0

    def require_writable_descriptor(descriptor: int) -> None:
        nonlocal verified_descriptors
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            try:
                os.write(descriptor, b"")
            except OSError as error:
                raise AssertionError("fsync descriptor is not writable on Windows") from error
        verified_descriptors += 1
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", require_writable_descriptor)
    try:
        backup_state(
            database,
            backup_root,
            account_state_path=account_state,
            created_at=datetime(2026, 8, 25, 3, tzinfo=UTC),
        )
    finally:
        database.close()

    assert verified_descriptors >= 3
