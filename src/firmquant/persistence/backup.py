"""Atomic SQLite/account-state backup bundles and isolated restore verification."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Never

from .audit import AuditLedger
from .database import Database, PersistenceError
from .repositories import canonical_json
from .schema import CURRENT_SCHEMA_VERSION


class BackupError(PersistenceError):
    """Raised when a consistent atomic backup cannot be created."""


class BackupVerificationError(BackupError):
    """Raised without deleting the backup evidence that failed verification."""


@dataclass(frozen=True, slots=True)
class BackupReceipt:
    backup_id: str
    bundle_path: Path
    database_sha256: str
    account_state_sha256: str | None
    manifest_sha256: str
    audit_count: int
    audit_head_hash: str
    created_at: str


@dataclass(frozen=True, slots=True)
class BackupVerification:
    backup_id: str
    database_sha256: str
    account_state_sha256: str | None
    manifest_sha256: str
    audit_count: int
    audit_head_hash: str
    schema_version: int


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_symlink() or not path.is_file():
        raise BackupVerificationError(f"backup member is not a regular file: {path.name}")
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise BackupVerificationError(f"cannot read backup member: {path.name}") from exc
    return digest.hexdigest()


def _write_fsynced(path: Path, content: bytes) -> None:
    try:
        with path.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            path.chmod(0o600)
    except OSError as exc:
        raise BackupError(f"cannot write backup member: {path.name}") from exc


def _copy_fsynced(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise BackupError("account state must be a regular non-symlink file")
    try:
        shutil.copyfile(source, destination)
        if os.name != "nt":
            destination.chmod(0o600)
        with destination.open("r+b") as stream:
            os.fsync(stream.fileno())
    except OSError as exc:
        raise BackupError("cannot copy account state into backup") from exc


def _reject_constant(value: str) -> Never:
    raise BackupVerificationError(f"backup manifest contains non-standard constant: {value}")


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BackupVerificationError(f"backup manifest contains duplicate key: {key}")
        result[key] = value
    return result


def _manifest(path: Path) -> dict[str, object]:
    try:
        payload: object = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupVerificationError("backup manifest is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise BackupVerificationError("backup manifest root must be an object")
    return payload


def _mapping(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise BackupVerificationError(f"backup manifest {label} must be an object")
    return value


def _text(mapping: dict[str, object], key: str, *, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise BackupVerificationError(f"backup manifest {label}.{key} must be text")
    return value


def _integer(mapping: dict[str, object], key: str, *, label: str) -> int:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise BackupVerificationError(f"backup manifest {label}.{key} must be integer")
    return value


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _database_schema_version(database: Database) -> int:
    value = database.scalar("SELECT max(version) FROM schema_migrations")
    if isinstance(value, bool) or not isinstance(value, int):
        raise BackupVerificationError("backup database schema version is missing or invalid")
    return value


def verify_backup(
    bundle_path: Path,
    *,
    expected_manifest_sha256: str | None = None,
) -> BackupVerification:
    """Restore a bundle in isolation and verify DB, audit head, account hash, and manifest."""

    bundle = Path(bundle_path)
    if bundle.is_symlink() or not bundle.is_dir():
        raise BackupVerificationError("backup bundle must be a regular directory")
    manifest_path = bundle / "manifest.json"
    manifest_sha256 = _sha256_file(manifest_path)
    if expected_manifest_sha256 is not None and manifest_sha256 != expected_manifest_sha256:
        raise BackupVerificationError("backup manifest SHA-256 does not match external receipt")
    manifest = _manifest(manifest_path)
    if set(manifest) != {
        "schema_version",
        "backup_id",
        "created_at",
        "database",
        "account_state",
        "operational_schema_version",
        "audit",
    }:
        raise BackupVerificationError("backup manifest fields do not match schema")
    if manifest["schema_version"] != 1:
        raise BackupVerificationError("unsupported backup manifest schema version")
    backup_id = _text(manifest, "backup_id", label="root")
    database_manifest = _mapping(manifest["database"], label="database")
    if set(database_manifest) != {"filename", "sha256"}:
        raise BackupVerificationError("backup database manifest fields do not match schema")
    if _text(database_manifest, "filename", label="database") != "firmquant.sqlite3":
        raise BackupVerificationError("backup database filename is not canonical")
    database_path = bundle / "firmquant.sqlite3"
    database_sha256 = _sha256_file(database_path)
    if database_sha256 != _text(database_manifest, "sha256", label="database"):
        raise BackupVerificationError("backup database SHA-256 mismatch")

    account_manifest = manifest["account_state"]
    account_state_sha256: str | None = None
    expected_names = {"manifest.json", "firmquant.sqlite3"}
    if account_manifest is not None:
        account_mapping = _mapping(account_manifest, label="account_state")
        if set(account_mapping) != {"filename", "sha256"}:
            raise BackupVerificationError("backup account manifest fields do not match schema")
        if _text(account_mapping, "filename", label="account_state") != "account_state.json":
            raise BackupVerificationError("backup account-state filename is not canonical")
        account_path = bundle / "account_state.json"
        account_state_sha256 = _sha256_file(account_path)
        if account_state_sha256 != _text(account_mapping, "sha256", label="account_state"):
            raise BackupVerificationError("backup account-state SHA-256 mismatch")
        expected_names.add("account_state.json")
    if {path.name for path in bundle.iterdir()} != expected_names:
        raise BackupVerificationError("backup bundle contains missing or unexpected members")

    audit_manifest = _mapping(manifest["audit"], label="audit")
    if set(audit_manifest) != {"count", "head_hash"}:
        raise BackupVerificationError("backup audit manifest fields do not match schema")
    expected_audit_count = _integer(audit_manifest, "count", label="audit")
    expected_audit_head = _text(audit_manifest, "head_hash", label="audit")
    expected_schema = manifest["operational_schema_version"]
    if isinstance(expected_schema, bool) or not isinstance(expected_schema, int):
        raise BackupVerificationError("backup operational schema version must be integer")

    with tempfile.TemporaryDirectory(prefix="firmquant-restore-verification-") as temporary:
        restored_path = Path(temporary) / "firmquant.sqlite3"
        shutil.copyfile(database_path, restored_path)
        try:
            restored = Database.open(restored_path)
            try:
                restored.integrity_check()
                schema_version = _database_schema_version(restored)
                verification = AuditLedger(restored).verify(
                    expected_count=expected_audit_count,
                    expected_head_hash=expected_audit_head,
                )
            finally:
                restored.close()
        except PersistenceError as exc:
            raise BackupVerificationError("isolated backup restore verification failed") from exc
    if schema_version != expected_schema or schema_version != CURRENT_SCHEMA_VERSION:
        raise BackupVerificationError("backup operational schema version mismatch")
    return BackupVerification(
        backup_id=backup_id,
        database_sha256=database_sha256,
        account_state_sha256=account_state_sha256,
        manifest_sha256=manifest_sha256,
        audit_count=verification.count,
        audit_head_hash=verification.head_hash,
        schema_version=schema_version,
    )


def backup_state(
    database: Database,
    destination_directory: Path,
    *,
    account_state_path: Path | None = None,
    created_at: datetime | None = None,
) -> BackupReceipt:
    """Create, verify, atomically publish, and receipt a complete state bundle."""

    root = Path(destination_directory)
    if root.is_symlink() or not root.is_dir():
        raise BackupError("backup destination must be a regular existing directory")
    timestamp = datetime.now(UTC) if created_at is None else created_at
    if not isinstance(timestamp, datetime) or timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise BackupError("backup created_at must be timezone-aware")
    utc_timestamp = timestamp.astimezone(UTC)
    backup_id = "backup-" + utc_timestamp.strftime("%Y%m%dT%H%M%S%fZ")
    final_bundle = root / backup_id
    if final_bundle.exists() or final_bundle.is_symlink():
        raise BackupError("refusing to overwrite an existing backup bundle")
    temporary_bundle = Path(tempfile.mkdtemp(prefix=f".{backup_id}.", dir=root))
    if os.name != "nt":
        temporary_bundle.chmod(0o700)

    database_path = temporary_bundle / "firmquant.sqlite3"
    database.backup_to(database_path)
    account_state_sha256: str | None = None
    account_manifest: dict[str, str] | None = None
    if account_state_path is not None:
        account_destination = temporary_bundle / "account_state.json"
        _copy_fsynced(Path(account_state_path), account_destination)
        account_state_sha256 = _sha256_file(account_destination)
        account_manifest = {
            "filename": "account_state.json",
            "sha256": account_state_sha256,
        }

    restored = Database.open(database_path)
    try:
        restored.integrity_check()
        audit = AuditLedger(restored).verify()
        schema_version = _database_schema_version(restored)
    finally:
        restored.close()
    database_sha256 = _sha256_file(database_path)
    manifest_payload = {
        "schema_version": 1,
        "backup_id": backup_id,
        "created_at": utc_timestamp.isoformat(),
        "database": {"filename": "firmquant.sqlite3", "sha256": database_sha256},
        "account_state": account_manifest,
        "operational_schema_version": schema_version,
        "audit": {"count": audit.count, "head_hash": audit.head_hash},
    }
    manifest_bytes = canonical_json(manifest_payload).encode("utf-8")
    manifest_path = temporary_bundle / "manifest.json"
    _write_fsynced(manifest_path, manifest_bytes)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    verify_backup(temporary_bundle, expected_manifest_sha256=manifest_sha256)

    try:
        os.replace(temporary_bundle, final_bundle)
        _fsync_directory(root)
    except OSError as exc:
        raise BackupError("cannot atomically publish verified backup bundle") from exc

    with database.transaction():
        database.write(
            """
            INSERT INTO backup_receipts(
                backup_id, database_sha256, account_state_sha256, manifest_json,
                manifest_sha256, created_at, verified_at, verification_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'VERIFIED')
            """,
            (
                backup_id,
                database_sha256,
                account_state_sha256,
                canonical_json(manifest_payload),
                manifest_sha256,
                utc_timestamp.isoformat(),
                datetime.now(UTC).isoformat(),
            ),
        )
    return BackupReceipt(
        backup_id=backup_id,
        bundle_path=final_bundle,
        database_sha256=database_sha256,
        account_state_sha256=account_state_sha256,
        manifest_sha256=manifest_sha256,
        audit_count=audit.count,
        audit_head_hash=audit.head_hash,
        created_at=utc_timestamp.isoformat(),
    )


__all__ = (
    "BackupError",
    "BackupReceipt",
    "BackupVerification",
    "BackupVerificationError",
    "backup_state",
    "verify_backup",
)
