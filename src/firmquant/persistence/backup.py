"""Atomic SQLite/account-state backup bundles and isolated restore verification."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Never, cast

from firmquant.application.production_identity import (
    DeploymentIdentity,
    IdentityError,
    OperationalEvidenceIdentity,
    deployment_caps_sha256,
    parse_identity,
    semantic_config_sha256,
)
from firmquant.broker.xtquant_safety import XtQuantSafetyManifest
from firmquant.config import Settings, load_settings
from firmquant.market_data.calendar_manifest import load_trading_calendar_manifest
from firmquant.risk.production_policy import ProductionSafetyPolicy

from .audit import AuditLedger
from .database import Database, PersistenceError
from .operational_authority import OperationalAuthorityStore
from .recovery import UquantAccountStateStore
from .repositories import PersistenceConflict, canonical_json
from .schema import CURRENT_SCHEMA_VERSION, MIGRATIONS


class BackupError(PersistenceError):
    """Raised when a consistent atomic backup cannot be created."""


class BackupVerificationError(BackupError):
    """Raised without deleting the backup evidence that failed verification."""


class BackupReason(StrEnum):
    """Exact production reasons accepted by schema-v3 recovery evidence."""

    SESSION_CLOSE = "SESSION_CLOSE"
    MODE_TRANSITION = "MODE_TRANSITION"
    ACCOUNT_REBASELINE = "ACCOUNT_REBASELINE"


_REASON_PHASE = {
    BackupReason.SESSION_CLOSE: "EOD",
    BackupReason.MODE_TRANSITION: "MODE_TRANSITION",
    BackupReason.ACCOUNT_REBASELINE: "ACCOUNT_REBASELINE",
}


@dataclass(frozen=True, slots=True)
class BackupBundleInputs:
    """Validated production identities copied into a complete recovery bundle."""

    settings: Settings
    config_path: Path
    config_sha256: str
    safety_manifest_path: Path
    calendar_manifest_path: Path
    active_data_manifest_path: Path
    strategy_data_manifest_path: Path
    firmquant_commit: str
    uquant_commit: str
    account_sha256: str
    decision_id: str | None
    strategy_session: date
    reason: BackupReason | None = None
    deployment_identity: DeploymentIdentity | None = None
    operational_evidence_identity: OperationalEvidenceIdentity | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.settings, Settings):
            raise TypeError("complete backup settings must be validated Settings")
        for label, value, length in (
            ("config SHA-256", self.config_sha256, 64),
            ("account SHA-256", self.account_sha256, 64),
            ("firmquant commit", self.firmquant_commit, 40),
            ("uquant commit", self.uquant_commit, 40),
        ):
            if (
                not isinstance(value, str)
                or len(value) != length
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"complete backup {label} is not canonical")
        if self.decision_id is not None and (not isinstance(self.decision_id, str) or not self.decision_id):
            raise ValueError("complete backup decision id is not canonical")
        if type(self.strategy_session) is not date:
            raise TypeError("complete backup strategy session must be date")
        for path in (
            self.config_path,
            self.safety_manifest_path,
            self.calendar_manifest_path,
            self.active_data_manifest_path,
            self.strategy_data_manifest_path,
        ):
            if not isinstance(path, Path):
                raise TypeError("complete backup member paths must be pathlib.Path")
        v3_values = (
            self.reason,
            self.deployment_identity,
            self.operational_evidence_identity,
        )
        if any(value is not None for value in v3_values) and any(value is None for value in v3_values):
            raise ValueError("schema-v3 backup identity inputs must be complete")
        if self.reason is not None and not isinstance(self.reason, BackupReason):
            raise TypeError("backup reason must be typed")
        if self.deployment_identity is not None and not isinstance(
            self.deployment_identity, DeploymentIdentity
        ):
            raise TypeError("backup deployment identity must be typed")
        if self.operational_evidence_identity is not None and not isinstance(
            self.operational_evidence_identity, OperationalEvidenceIdentity
        ):
            raise TypeError("backup operational evidence identity must be typed")
        if self.deployment_identity is not None and self.operational_evidence_identity is not None:
            if self.operational_evidence_identity.deployment_identity != self.deployment_identity:
                raise ValueError("backup identities do not share the same deployment payload")
            if self.deployment_identity.firmquant_commit != self.firmquant_commit:
                raise ValueError("backup firmquant commit differs from deployment identity")
            if self.deployment_identity.uquant_commit != self.uquant_commit:
                raise ValueError("backup uquant commit differs from deployment identity")
            if self.deployment_identity.raw_config_sha256 != self.config_sha256:
                raise ValueError("backup config digest differs from deployment identity")
            if self.operational_evidence_identity.account_state_sha256 != self.account_sha256:
                raise ValueError("backup account state differs from operational evidence")
            if self.operational_evidence_identity.strategy_session != self.strategy_session:
                raise ValueError("backup strategy session differs from operational evidence")
            if self.operational_evidence_identity.decision_id != self.decision_id:
                raise ValueError("backup decision differs from operational evidence")
            if self.deployment_identity.mode is not self.settings.mode:
                raise ValueError("backup deployment mode differs from validated settings")
            expected_phase = _REASON_PHASE[cast(BackupReason, self.reason)]
            if (
                self.operational_evidence_identity.phase != expected_phase
                or self.operational_evidence_identity.kind != "BACKUP"
            ):
                raise ValueError("backup reason phase/kind facts do not match")
            if self.reason is BackupReason.SESSION_CLOSE and self.decision_id is None:
                raise ValueError("SESSION_CLOSE backup requires a frozen decision")
        elif self.decision_id is None:
            raise ValueError("schema-v2 complete backup requires a frozen decision")


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
    schema_version: int = 1
    production_authority: bool = False


@dataclass(frozen=True, slots=True)
class BackupVerification:
    backup_id: str
    database_sha256: str
    account_state_sha256: str | None
    manifest_sha256: str
    audit_count: int
    audit_head_hash: str
    schema_version: int
    operational_schema_version: int
    complete_bundle: bool = False
    production_authority: bool = False
    decision_id: str | None = None
    reason: BackupReason | None = None
    deployment_identity_sha256: str | None = None
    operational_evidence_identity_sha256: str | None = None
    account_authority_epoch: int | None = None
    mode_epoch: int | None = None
    broker_snapshot_id: str | None = None
    broker_snapshot_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class RestoreReceipt:
    restore_id: str
    source_backup_id: str
    destination: Path
    source_manifest_sha256: str
    sanitized_state_sha256: str
    original_audit_count: int
    original_audit_head: str
    restored_audit_count: int
    restored_audit_head: str
    restored_at: str
    requires_fresh_snapshot: bool = True
    requires_reconciliation: bool = True


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


def _copy_fsynced(source: Path, destination: Path, *, label: str) -> None:
    if source.is_symlink() or not source.is_file():
        raise BackupError(f"{label} must be a regular non-symlink file")
    try:
        shutil.copyfile(source, destination)
        if os.name != "nt":
            destination.chmod(0o600)
        with destination.open("r+b") as stream:
            os.fsync(stream.fileno())
    except OSError as exc:
        raise BackupError(f"cannot copy {label} into backup") from exc


def _stable_file_identity(path: Path) -> tuple[int, int, int, int]:
    try:
        value = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise BackupVerificationError(f"cannot stat backup member: {path.name}") from exc
    if path.is_symlink() or not stat.S_ISREG(value.st_mode):
        raise BackupVerificationError(f"backup member is not stable regular file: {path.name}")
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _copy_verified_member(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str,
    label: str,
) -> None:
    before = _stable_file_identity(source)
    if _sha256_file(source) != expected_sha256:
        raise BackupVerificationError(f"{label} source SHA-256 changed")
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_file():
            raise BackupVerificationError(f"{label} staging member is not a regular file")
        if _sha256_file(destination) != expected_sha256:
            raise BackupVerificationError(f"{label} staging member conflicts with source")
    else:
        temporary = destination.parent / f".{destination.name}.copying"
        if temporary.exists() or temporary.is_symlink():
            if temporary.is_symlink() or not temporary.is_file():
                raise BackupVerificationError(f"{label} partial copy is not regular evidence")
            source_bytes = source.read_bytes()
            partial = temporary.read_bytes()
            if len(partial) > len(source_bytes) or source_bytes[: len(partial)] != partial:
                raise BackupVerificationError(f"{label} partial copy conflicts with source")
            try:
                with temporary.open("ab") as stream:
                    stream.write(source_bytes[len(partial) :])
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as exc:
                raise BackupError(f"cannot resume {label} partial copy") from exc
        else:
            _copy_fsynced(source, temporary, label=label)
        if _sha256_file(temporary) != expected_sha256:
            raise BackupVerificationError(f"{label} copied SHA-256 is invalid")
        try:
            os.replace(temporary, destination)
        except OSError as exc:
            raise BackupError(f"cannot commit {label} private copy") from exc
    after = _stable_file_identity(source)
    if after != before or _sha256_file(source) != expected_sha256:
        raise BackupVerificationError(f"{label} source changed during copy")


def _ensure_fsynced_content(path: Path, content: bytes, *, label: str) -> None:
    expected_sha256 = hashlib.sha256(content).hexdigest()
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or _sha256_file(path) != expected_sha256:
            raise BackupVerificationError(f"{label} staging member conflicts")
        return
    temporary = path.parent / f".{path.name}.copying"
    if temporary.exists() or temporary.is_symlink():
        if temporary.is_symlink() or not temporary.is_file():
            raise BackupVerificationError(f"{label} partial copy is not regular evidence")
        partial = temporary.read_bytes()
        if len(partial) > len(content) or content[: len(partial)] != partial:
            raise BackupVerificationError(f"{label} partial copy conflicts")
        try:
            with temporary.open("ab") as stream:
                stream.write(content[len(partial) :])
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise BackupError(f"cannot resume {label} partial content") from exc
    else:
        _write_fsynced(temporary, content)
    if _sha256_file(temporary) != expected_sha256:
        raise BackupVerificationError(f"{label} partial content digest is invalid")
    try:
        os.replace(temporary, path)
    except OSError as exc:
        raise BackupError(f"cannot commit {label} private content") from exc


def _reject_constant(value: str) -> Never:
    raise BackupVerificationError(f"backup manifest contains non-standard constant: {value}")


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BackupVerificationError(f"backup manifest contains duplicate key: {key}")
        result[key] = value
    return result


def _json_object(path: Path, *, label: str) -> dict[str, object]:
    try:
        payload: object = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupVerificationError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise BackupVerificationError(f"{label} root must be an object")
    return payload


def _manifest(path: Path) -> dict[str, object]:
    return _json_object(path, label="backup manifest")


def _mapping(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise BackupVerificationError(f"backup manifest {label} must be an object")
    return value


def _text(mapping: Mapping[str, object], key: str, *, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise BackupVerificationError(f"backup manifest {label}.{key} must be text")
    return value


def _optional_text(mapping: Mapping[str, object], key: str, *, label: str) -> str | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise BackupVerificationError(f"backup manifest {label}.{key} must be text or null")
    return value


def _integer(mapping: Mapping[str, object], key: str, *, label: str) -> int:
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


_MOVEFILE_WRITE_THROUGH = 0x8


def _move_file_ex(source: Path, destination: Path, flags: int) -> bool:
    loader = getattr(ctypes, "WinDLL", None)
    last_error = getattr(ctypes, "get_last_error", None)
    if loader is None or last_error is None:
        raise OSError("MoveFileExW is unavailable")
    kernel32: Any = loader("kernel32", use_last_error=True)
    move_file_ex: Any = kernel32.MoveFileExW
    move_file_ex.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    move_file_ex.restype = ctypes.c_int
    result = bool(move_file_ex(str(source), str(destination), flags))
    if not result:
        error = int(last_error())
        raise OSError(error, "MoveFileExW failed")
    return True


def _publish_directory(
    source: Path,
    destination: Path,
    *,
    platform_name: str | None = None,
) -> None:
    platform = os.name if platform_name is None else platform_name
    try:
        source_parent = source.parent.resolve(strict=True)
        destination_parent = destination.parent.resolve(strict=True)
    except OSError as exc:
        raise BackupError("publication parent cannot be resolved") from exc
    if source_parent != destination_parent:
        raise BackupError("publication requires the same parent and volume")
    if source.is_symlink() or not source.is_dir() or destination.exists() or destination.is_symlink():
        raise BackupError("publication source or destination identity is invalid")
    if platform == "nt":
        try:
            if not _move_file_ex(source, destination, _MOVEFILE_WRITE_THROUGH):
                raise OSError("MoveFileExW returned false")
        except OSError as exc:
            raise BackupError("Windows write-through directory publication failed") from exc
        return
    try:
        os.replace(source, destination)
        _fsync_directory(destination_parent)
    except OSError as exc:
        raise BackupError("atomic directory publish failed") from exc


def _database_schema_version(database: Database) -> int:
    value = database.scalar("SELECT max(version) FROM schema_migrations")
    if isinstance(value, bool) or not isinstance(value, int):
        raise BackupVerificationError("backup database schema version is missing or invalid")
    return value


def _verify_migration_prefix(database: Database, expected_schema: int) -> None:
    if expected_schema < 1 or expected_schema > CURRENT_SCHEMA_VERSION:
        raise BackupVerificationError("backup operational schema version is unsupported")
    rows = database.query_all("SELECT version,name,checksum FROM schema_migrations ORDER BY version")
    expected = MIGRATIONS[:expected_schema]
    observed = tuple((int(row["version"]), str(row["name"]), str(row["checksum"])) for row in rows)
    canonical = tuple((item.version, item.name, item.checksum) for item in expected)
    if observed != canonical:
        raise BackupVerificationError("backup migration history is not a contiguous known prefix")


def _verify_database(
    database_path: Path,
    *,
    expected_audit_count: int,
    expected_audit_head: str,
    expected_schema: int,
    required_decision_id: str | None = None,
) -> tuple[int, int, str]:
    try:
        restored = Database.open_read_only(database_path, immutable=True)
        try:
            restored.integrity_check()
            schema_version = _database_schema_version(restored)
            _verify_migration_prefix(restored, expected_schema)
            audit = AuditLedger(restored).verify(
                expected_count=expected_audit_count,
                expected_head_hash=expected_audit_head,
            )
            if required_decision_id is not None:
                decision = restored.query_one(
                    "SELECT decision_id FROM decision_snapshots WHERE decision_id = ?",
                    (required_decision_id,),
                )
                if decision is None:
                    raise BackupVerificationError("complete backup database lacks required frozen decision")
        finally:
            restored.close()
    except PersistenceError as exc:
        raise BackupVerificationError("isolated backup read-only verification failed") from exc
    if schema_version != expected_schema:
        raise BackupVerificationError("backup operational schema version mismatch")
    return schema_version, audit.count, audit.head_hash


def _verify_legacy_bundle(
    bundle: Path,
    manifest: dict[str, object],
    *,
    manifest_sha256: str,
) -> BackupVerification:
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
    schema_version, audit_count, audit_head = _verify_database(
        database_path,
        expected_audit_count=expected_audit_count,
        expected_audit_head=expected_audit_head,
        expected_schema=expected_schema,
    )
    return BackupVerification(
        backup_id=backup_id,
        database_sha256=database_sha256,
        account_state_sha256=account_state_sha256,
        manifest_sha256=manifest_sha256,
        audit_count=audit_count,
        audit_head_hash=audit_head,
        schema_version=_integer(manifest, "schema_version", label="root"),
        operational_schema_version=schema_version,
    )


def _verify_complete_bundle(
    bundle: Path,
    manifest: dict[str, object],
    *,
    manifest_sha256: str,
) -> BackupVerification:
    if set(manifest) != {
        "schema_version",
        "backup_id",
        "created_at",
        "members",
        "operational_schema_version",
        "audit",
        "deployment",
    }:
        raise BackupVerificationError("complete backup manifest fields do not match schema")
    members = _mapping(manifest["members"], label="members")
    required = {
        "firmquant.sqlite3",
        "account_state.json",
        "production_config.toml",
        "xtquant_safety_manifest.json",
        "trading_calendar.json",
        "active_data_source.json",
        "strategy_data_manifest.json",
        "deployment_record.json",
    }
    if set(members) != required:
        raise BackupVerificationError("complete backup member set does not match contract")
    if {path.name for path in bundle.iterdir()} != required | {"manifest.json"}:
        raise BackupVerificationError("complete backup contains missing or unexpected members")
    member_hashes: dict[str, str] = {}
    for name in sorted(required):
        expected = members[name]
        if not isinstance(expected, str) or len(expected) != 64:
            raise BackupVerificationError("complete backup member digest is invalid")
        observed = _sha256_file(bundle / name)
        if observed != expected:
            raise BackupVerificationError(f"complete backup member SHA-256 mismatch: {name}")
        member_hashes[name] = observed

    try:
        load_settings(bundle / "production_config.toml")
    except Exception as exc:
        raise BackupVerificationError("complete backup production config is invalid") from exc
    load_trading_calendar_manifest(bundle / "trading_calendar.json")
    XtQuantSafetyManifest.load(bundle / "xtquant_safety_manifest.json")
    active_data = _json_object(bundle / "active_data_source.json", label="active data source")
    if not active_data:
        raise BackupVerificationError("complete backup active data identity is empty")
    strategy_data = _json_object(
        bundle / "strategy_data_manifest.json",
        label="strategy data manifest",
    )
    if not strategy_data:
        raise BackupVerificationError("complete backup strategy data identity is empty")
    deployment = _json_object(bundle / "deployment_record.json", label="deployment record")
    manifest_deployment = _mapping(manifest["deployment"], label="deployment")
    if deployment != manifest_deployment:
        raise BackupVerificationError("complete backup deployment identity changed")
    decision_id = _text(deployment, "decision_id", label="deployment")
    account_sha256 = _text(deployment, "account_sha256", label="deployment")
    try:
        authority_hash = UquantAccountStateStore().hash_file(bundle / "account_state.json")
    except Exception as exc:
        raise BackupVerificationError("complete backup account authority is invalid") from exc
    if account_sha256 != authority_hash:
        raise BackupVerificationError("complete backup account identity is inconsistent")
    if _text(deployment, "config_sha256", label="deployment") != member_hashes["production_config.toml"]:
        raise BackupVerificationError("complete backup config identity is inconsistent")
    if _text(deployment, "calendar_sha256", label="deployment") != member_hashes["trading_calendar.json"]:
        raise BackupVerificationError("complete backup calendar identity is inconsistent")
    if (
        _text(deployment, "active_data_manifest_sha256", label="deployment")
        != member_hashes["active_data_source.json"]
    ):
        raise BackupVerificationError("complete backup active-data identity is inconsistent")
    if (
        _text(deployment, "strategy_data_manifest_sha256", label="deployment")
        != member_hashes["strategy_data_manifest.json"]
    ):
        raise BackupVerificationError("complete backup strategy-data identity is inconsistent")

    audit_manifest = _mapping(manifest["audit"], label="audit")
    expected_audit_count = _integer(audit_manifest, "count", label="audit")
    expected_audit_head = _text(audit_manifest, "head_hash", label="audit")
    expected_schema = manifest["operational_schema_version"]
    if isinstance(expected_schema, bool) or not isinstance(expected_schema, int):
        raise BackupVerificationError("backup operational schema version must be integer")
    database_path = bundle / "firmquant.sqlite3"
    schema_version, audit_count, audit_head = _verify_database(
        database_path,
        expected_audit_count=expected_audit_count,
        expected_audit_head=expected_audit_head,
        expected_schema=expected_schema,
        required_decision_id=decision_id,
    )
    return BackupVerification(
        backup_id=_text(manifest, "backup_id", label="root"),
        database_sha256=member_hashes["firmquant.sqlite3"],
        account_state_sha256=member_hashes["account_state.json"],
        manifest_sha256=manifest_sha256,
        audit_count=audit_count,
        audit_head_hash=audit_head,
        schema_version=2,
        operational_schema_version=schema_version,
        complete_bundle=True,
        production_authority=False,
        decision_id=decision_id,
    )


_V3_MEMBERS = frozenset(
    {
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
    }
)


def _canonical_utc_text(value: object, *, label: str) -> str:
    text = value if isinstance(value, str) else ""
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise BackupVerificationError(f"backup {label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BackupVerificationError(f"backup {label} must be timezone-aware")
    canonical = parsed.astimezone(UTC).isoformat()
    if text != canonical:
        raise BackupVerificationError(f"backup {label} is not canonical UTC")
    return text


def _lower_digest(value: object, *, label: str, length: int = 64) -> str:
    if (
        not isinstance(value, str)
        or len(value) != length
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise BackupVerificationError(f"backup {label} is not a canonical lowercase digest")
    return value


def _canonical_object(path: Path, *, label: str) -> dict[str, object]:
    payload = _json_object(path, label=label)
    try:
        canonical = canonical_json(payload).encode("utf-8")
        rendered = path.read_bytes()
    except (OSError, TypeError, UnicodeError) as exc:
        raise BackupVerificationError(f"{label} is not strict canonical JSON") from exc
    if rendered != canonical:
        raise BackupVerificationError(f"{label} is not strict canonical JSON")
    return payload


def _validate_data_manifests(active_path: Path, strategy_path: Path) -> None:
    active = _canonical_object(active_path, label="active data source manifest")
    if (
        set(active)
        != {
            "schema",
            "generation_id",
            "source",
            "created_at",
            "members",
            "data_sha256",
        }
        or active.get("schema") != "firmquant.data-generation.v1"
    ):
        raise BackupVerificationError("active data source manifest contract is invalid")
    generation_id = _text(active, "generation_id", label="active data source")
    if (
        len(generation_id) != 28
        or not generation_id.startswith("gen-")
        or any(character not in "0123456789abcdef" for character in generation_id[4:])
    ):
        raise BackupVerificationError("active data generation id is invalid")
    source = _text(active, "source", label="active data source")
    if source != source.strip():
        raise BackupVerificationError("active data source name is not canonical")
    _canonical_utc_text(active["created_at"], label="active data creation time")
    members = _mapping(active["members"], label="active data members")
    if not members:
        raise BackupVerificationError("active data generation has no members")
    for name, digest in members.items():
        if (
            Path(name).name != name
            or not name.endswith(".csv")
            or _lower_digest(digest, label=f"active data member {name}") != digest
        ):
            raise BackupVerificationError("active data generation member is invalid")
    expected_data_sha256 = hashlib.sha256(
        canonical_json(dict(sorted(members.items()))).encode("utf-8")
    ).hexdigest()
    if _lower_digest(active["data_sha256"], label="active data digest") != expected_data_sha256:
        raise BackupVerificationError("active data generation digest is invalid")

    strategy = _canonical_object(strategy_path, label="strategy data manifest")
    if (
        set(strategy)
        != {
            "schema",
            "target_session",
            "source",
            "uquant_manifest_sha256",
            "data_generation_id",
            "observations",
        }
        or strategy.get("schema") != "firmquant.daily-data-manifest.v2"
    ):
        raise BackupVerificationError("strategy data manifest contract is invalid")
    target_text = _text(strategy, "target_session", label="strategy data")
    try:
        target = date.fromisoformat(target_text)
    except ValueError as exc:
        raise BackupVerificationError("strategy data target session is invalid") from exc
    if target.isoformat() != target_text or strategy.get("source") != "xtquant":
        raise BackupVerificationError("strategy data manifest source/session is invalid")
    _lower_digest(strategy["uquant_manifest_sha256"], label="uquant data manifest")
    if strategy.get("data_generation_id") != generation_id:
        raise BackupVerificationError("strategy data generation identity is inconsistent")
    observations = strategy.get("observations")
    if not isinstance(observations, list) or not observations:
        raise BackupVerificationError("strategy data observations are invalid")
    observed_symbols: list[str] = []
    for value in observations:
        observation = _mapping(value, label="strategy data observation")
        if set(observation) != {
            "symbol",
            "latest_observed_session",
            "suspension_evidence_sha256",
        }:
            raise BackupVerificationError("strategy data observation contract is invalid")
        symbol = _text(observation, "symbol", label="strategy data observation")
        if len(symbol) != 8 or symbol[:2] not in {"sh", "sz", "bj"} or not symbol[2:].isdigit():
            raise BackupVerificationError("strategy data observation symbol is invalid")
        latest_text = _text(observation, "latest_observed_session", label="strategy data observation")
        try:
            latest = date.fromisoformat(latest_text)
        except ValueError as exc:
            raise BackupVerificationError("strategy data observation session is invalid") from exc
        suspension = observation.get("suspension_evidence_sha256")
        if latest > target or ((latest == target) != (suspension is None)):
            raise BackupVerificationError("strategy data observation timing is invalid")
        if suspension is not None:
            _lower_digest(suspension, label="strategy data suspension evidence")
        observed_symbols.append(symbol)
    if observed_symbols != sorted(set(observed_symbols)):
        raise BackupVerificationError("strategy data observations are not canonical")


def _verify_v3_database_bindings(
    database_path: Path,
    *,
    deployment: DeploymentIdentity,
    evidence: OperationalEvidenceIdentity,
    account_epoch_payload: Mapping[str, object],
    mode_epoch_payload: Mapping[str, object],
) -> None:
    database = Database.open_read_only(database_path, immutable=True)
    try:
        try:
            authority = OperationalAuthorityStore(database)
            account = authority.active_account_epoch()
            mode = authority.active_mode_epoch()
        except PersistenceConflict as exc:
            raise BackupVerificationError("backup typed authority epoch is invalid") from exc
        expected_account = {
            "schema": "firmquant.account-authority-epoch-backup.v1",
            "epoch": account.epoch,
            "account_id_hash": account.account_id_hash,
            "account_state_sha256": account.account_state_sha256,
            "deployment_identity_sha256": account.deployment_identity_sha256,
            "payload": _mapping(json.loads(account.payload_json), label="account authority payload"),
            "payload_sha256": account.payload_sha256,
            "created_at": account.created_at.isoformat(),
        }
        expected_mode = {
            "schema": "firmquant.mode-epoch-backup.v1",
            "epoch": mode.epoch,
            "mode": mode.mode.value,
            "deployment_identity_sha256": mode.deployment_identity_sha256,
            "caps_sha256": mode.caps_sha256,
            "payload": _mapping(json.loads(mode.payload_json), label="mode epoch payload"),
            "payload_sha256": mode.payload_sha256,
            "created_at": mode.created_at.isoformat(),
        }
        if dict(account_epoch_payload) != expected_account or dict(mode_epoch_payload) != expected_mode:
            raise BackupVerificationError("backup authority epoch member differs from database")
        if (
            account.epoch != deployment.account_authority_epoch
            or account.account_id_hash != deployment.account_id_hash
            or account.deployment_identity_sha256 != deployment.sha256
            or mode.epoch != deployment.mode_epoch
            or mode.mode is not deployment.mode
            or mode.deployment_identity_sha256 != deployment.sha256
            or mode.caps_sha256 != deployment.caps_sha256
        ):
            raise BackupVerificationError("backup authority epoch differs from deployment identity")
        identity = database.query_one(
            """
            SELECT account_id_hash,account_authority_epoch,mode_epoch,mode,payload_json,payload_sha256
            FROM deployment_identities WHERE deployment_identity_sha256=?
            """,
            (deployment.sha256,),
        )
        if identity is None or (
            str(identity["account_id_hash"]),
            int(identity["account_authority_epoch"]),
            int(identity["mode_epoch"]),
            str(identity["mode"]),
            str(identity["payload_json"]),
            str(identity["payload_sha256"]),
        ) != (
            deployment.account_id_hash,
            deployment.account_authority_epoch,
            deployment.mode_epoch,
            deployment.mode.value,
            deployment.canonical_json,
            deployment.sha256,
        ):
            raise BackupVerificationError("backup deployment identity differs from database")
        observation = database.query_one(
            """
            SELECT deployment_identity_sha256,account_authority_epoch,mode_epoch,
                   account_state_sha256,broker_snapshot_id,strategy_session,phase,kind,
                   payload_json,payload_sha256
            FROM operational_evidence_receipts
            WHERE operational_evidence_identity_sha256=?
            """,
            (evidence.sha256,),
        )
        if observation is None or (
            str(observation["deployment_identity_sha256"]),
            int(observation["account_authority_epoch"]),
            int(observation["mode_epoch"]),
            str(observation["account_state_sha256"]),
            str(observation["broker_snapshot_id"]),
            str(observation["strategy_session"]),
            str(observation["phase"]),
            str(observation["kind"]),
            str(observation["payload_json"]),
            str(observation["payload_sha256"]),
        ) != (
            deployment.sha256,
            deployment.account_authority_epoch,
            deployment.mode_epoch,
            evidence.account_state_sha256,
            evidence.broker_snapshot_id,
            evidence.strategy_session.isoformat(),
            evidence.phase,
            evidence.kind,
            evidence.canonical_json,
            evidence.sha256,
        ):
            raise BackupVerificationError("backup operational evidence differs from database")
        snapshot = database.query_one(
            """
            SELECT account_id_hash,account_type,broker_event_watermark,raw_payload_sha256,
                   complete,started_at,completed_at,duration_ms
            FROM broker_snapshots WHERE snapshot_id=?
            """,
            (evidence.broker_snapshot_id,),
        )
        cash = database.query_one(
            "SELECT snapshot_id FROM cash_snapshots WHERE snapshot_id=?",
            (evidence.broker_snapshot_id,),
        )
        if (
            snapshot is None
            or cash is None
            or (
                str(snapshot["account_id_hash"]),
                str(snapshot["account_type"]),
                int(snapshot["broker_event_watermark"]),
                str(snapshot["raw_payload_sha256"]),
                int(snapshot["complete"]),
                str(snapshot["started_at"]),
                str(snapshot["completed_at"]),
                int(snapshot["duration_ms"]),
            )
            != (
                deployment.account_id_hash,
                "CASH",
                evidence.broker_event_watermark,
                evidence.broker_snapshot_sha256,
                1,
                evidence.snapshot_started_at.isoformat(),
                evidence.snapshot_completed_at.isoformat(),
                evidence.snapshot_duration_ms,
            )
        ):
            raise BackupVerificationError("backup broker snapshot identity differs from database")
    finally:
        database.close()


def _verify_v3_bundle(
    bundle: Path,
    manifest: dict[str, object],
    *,
    manifest_sha256: str,
    enforce_directory_name: bool,
    expected_backup_id: str | None = None,
) -> BackupVerification:
    try:
        manifest_bytes = (bundle / "manifest.json").read_bytes()
    except OSError as exc:
        raise BackupVerificationError("schema-v3 backup manifest cannot be read") from exc
    if manifest_bytes != canonical_json(manifest).encode("utf-8"):
        raise BackupVerificationError("schema-v3 backup manifest is not canonical JSON")
    expected_fields = {
        "schema_version",
        "backup_id",
        "created_at",
        "reason",
        "members",
        "operational_schema_version",
        "audit",
        "deployment_identity_sha256",
        "operational_evidence_identity_sha256",
        "account_state_sha256",
        "account_authority_epoch",
        "mode_epoch",
        "broker_snapshot_id",
        "broker_snapshot_sha256",
        "broker_event_watermark",
        "strategy_session",
        "decision_id",
    }
    if set(manifest) != expected_fields:
        raise BackupVerificationError("schema-v3 backup manifest fields do not match contract")
    _canonical_utc_text(manifest["created_at"], label="creation time")
    try:
        reason = BackupReason(_text(manifest, "reason", label="root"))
    except ValueError as exc:
        raise BackupVerificationError("schema-v3 backup reason is invalid") from exc
    members = _mapping(manifest["members"], label="members")
    if set(members) != _V3_MEMBERS:
        raise BackupVerificationError("schema-v3 backup member set does not match contract")
    if {path.name for path in bundle.iterdir()} != _V3_MEMBERS | {"manifest.json"}:
        raise BackupVerificationError("schema-v3 backup contains missing or unexpected members")
    member_hashes: dict[str, str] = {}
    for name in sorted(_V3_MEMBERS):
        expected = _lower_digest(members[name], label=f"member digest {name}")
        observed = _sha256_file(bundle / name)
        if observed != expected:
            raise BackupVerificationError(f"schema-v3 backup member SHA-256 mismatch: {name}")
        member_hashes[name] = observed
    try:
        deployment_value = parse_identity(
            (bundle / "deployment_identity.json").read_bytes(),
            expected_sha256=_lower_digest(
                manifest["deployment_identity_sha256"], label="deployment identity"
            ),
        )
        evidence_value = parse_identity(
            (bundle / "operational_evidence_identity.json").read_bytes(),
            expected_sha256=_lower_digest(
                manifest["operational_evidence_identity_sha256"],
                label="operational evidence identity",
            ),
        )
    except (OSError, IdentityError) as exc:
        raise BackupVerificationError("schema-v3 backup identity is invalid") from exc
    if not isinstance(deployment_value, DeploymentIdentity) or not isinstance(
        evidence_value, OperationalEvidenceIdentity
    ):
        raise BackupVerificationError("schema-v3 backup identity kinds are invalid")
    deployment = deployment_value
    evidence = evidence_value
    if evidence.deployment_identity != deployment:
        raise BackupVerificationError("schema-v3 operational identity deployment changed")
    backup_id = _text(manifest, "backup_id", label="root")
    deterministic_backup_id = _v3_backup_id_from_identities(reason, deployment, evidence)
    if backup_id != deterministic_backup_id or (
        expected_backup_id is not None and backup_id != expected_backup_id
    ):
        raise BackupVerificationError("schema-v3 backup id is not deterministic")
    if enforce_directory_name and bundle.name != backup_id:
        raise BackupVerificationError("schema-v3 backup directory basename differs from backup id")
    account_state_sha256 = _lower_digest(manifest["account_state_sha256"], label="account state identity")
    try:
        observed_account_state = UquantAccountStateStore().hash_file(bundle / "account_state.json")
    except Exception as exc:
        raise BackupVerificationError("schema-v3 AccountState is not strict current uquant state") from exc
    if (
        observed_account_state != account_state_sha256
        or evidence.account_state_sha256 != account_state_sha256
    ):
        raise BackupVerificationError("schema-v3 account state identity is inconsistent")
    if deployment.account_authority_epoch != _integer(manifest, "account_authority_epoch", label="root"):
        raise BackupVerificationError("schema-v3 account authority epoch is inconsistent")
    if deployment.mode_epoch != _integer(manifest, "mode_epoch", label="root"):
        raise BackupVerificationError("schema-v3 mode epoch is inconsistent")
    if evidence.broker_snapshot_id != _text(manifest, "broker_snapshot_id", label="root"):
        raise BackupVerificationError("schema-v3 broker snapshot id is inconsistent")
    broker_snapshot_sha256 = _lower_digest(
        manifest["broker_snapshot_sha256"], label="broker snapshot identity"
    )
    if evidence.broker_snapshot_sha256 != broker_snapshot_sha256:
        raise BackupVerificationError("schema-v3 broker snapshot digest is inconsistent")
    if evidence.broker_event_watermark != _integer(manifest, "broker_event_watermark", label="root"):
        raise BackupVerificationError("schema-v3 broker watermark is inconsistent")
    if evidence.strategy_session.isoformat() != _text(manifest, "strategy_session", label="root"):
        raise BackupVerificationError("schema-v3 strategy session is inconsistent")
    if evidence.decision_id != _optional_text(manifest, "decision_id", label="root"):
        raise BackupVerificationError("schema-v3 decision identity is inconsistent")
    try:
        settings = load_settings(bundle / "production_config.toml")
        XtQuantSafetyManifest.load(bundle / "xtquant_safety_manifest.json")
        load_trading_calendar_manifest(bundle / "trading_calendar.json")
        _validate_data_manifests(
            bundle / "active_data_source.json",
            bundle / "strategy_data_manifest.json",
        )
    except Exception as exc:
        raise BackupVerificationError("schema-v3 validated configuration evidence is invalid") from exc
    if deployment.raw_config_sha256 != member_hashes["production_config.toml"]:
        raise BackupVerificationError("schema-v3 raw config identity is inconsistent")
    if deployment.semantic_config_sha256 != semantic_config_sha256(settings):
        raise BackupVerificationError("schema-v3 semantic config identity is inconsistent")
    if deployment.caps_sha256 != deployment_caps_sha256(settings):
        raise BackupVerificationError("schema-v3 deployment caps identity is inconsistent")
    if deployment.production_policy_sha256 != ProductionSafetyPolicy.from_settings(settings).sha256:
        raise BackupVerificationError("schema-v3 production policy identity is inconsistent")
    if deployment.mode is not settings.mode:
        raise BackupVerificationError("schema-v3 deployment mode differs from validated settings")
    if (
        evidence.phase != _REASON_PHASE[reason]
        or evidence.kind != "BACKUP"
        or (reason is BackupReason.SESSION_CLOSE and evidence.decision_id is None)
    ):
        raise BackupVerificationError("schema-v3 reason-specific operational facts are invalid")
    if deployment.xtquant_safety_manifest_sha256 != member_hashes["xtquant_safety_manifest.json"]:
        raise BackupVerificationError("schema-v3 XtQuant safety identity is inconsistent")
    if evidence.calendar_sha256 != member_hashes["trading_calendar.json"]:
        raise BackupVerificationError("schema-v3 calendar identity is inconsistent")
    if evidence.active_data_generation_sha256 != member_hashes["active_data_source.json"]:
        raise BackupVerificationError("schema-v3 active data identity is inconsistent")
    if evidence.strategy_data_manifest_sha256 != member_hashes["strategy_data_manifest.json"]:
        raise BackupVerificationError("schema-v3 strategy data identity is inconsistent")
    account_epoch = _json_object(bundle / "account_authority_epoch.json", label="account authority epoch")
    mode_epoch = _json_object(bundle / "mode_epoch.json", label="mode epoch")
    if (bundle / "account_authority_epoch.json").read_bytes() != canonical_json(account_epoch).encode(
        "utf-8"
    ):
        raise BackupVerificationError("schema-v3 account authority epoch is not canonical JSON")
    if (bundle / "mode_epoch.json").read_bytes() != canonical_json(mode_epoch).encode("utf-8"):
        raise BackupVerificationError("schema-v3 mode epoch is not canonical JSON")
    audit_manifest = _mapping(manifest["audit"], label="audit")
    if set(audit_manifest) != {"count", "head_hash"}:
        raise BackupVerificationError("schema-v3 audit identity is invalid")
    expected_audit_count = _integer(audit_manifest, "count", label="audit")
    expected_audit_head = _lower_digest(audit_manifest["head_hash"], label="audit head")
    operational_schema = _integer(manifest, "operational_schema_version", label="root")
    if operational_schema != CURRENT_SCHEMA_VERSION:
        raise BackupVerificationError("schema-v3 backup requires current operational schema")
    database_path = bundle / "firmquant.sqlite3"
    _, audit_count, audit_head = _verify_database(
        database_path,
        expected_audit_count=expected_audit_count,
        expected_audit_head=expected_audit_head,
        expected_schema=operational_schema,
        required_decision_id=evidence.decision_id,
    )
    _verify_v3_database_bindings(
        database_path,
        deployment=deployment,
        evidence=evidence,
        account_epoch_payload=account_epoch,
        mode_epoch_payload=mode_epoch,
    )
    return BackupVerification(
        backup_id=backup_id,
        database_sha256=member_hashes["firmquant.sqlite3"],
        account_state_sha256=account_state_sha256,
        manifest_sha256=manifest_sha256,
        audit_count=audit_count,
        audit_head_hash=audit_head,
        schema_version=3,
        operational_schema_version=operational_schema,
        complete_bundle=True,
        production_authority=True,
        decision_id=evidence.decision_id,
        reason=reason,
        deployment_identity_sha256=deployment.sha256,
        operational_evidence_identity_sha256=evidence.sha256,
        account_authority_epoch=deployment.account_authority_epoch,
        mode_epoch=deployment.mode_epoch,
        broker_snapshot_id=evidence.broker_snapshot_id,
        broker_snapshot_sha256=evidence.broker_snapshot_sha256,
    )


def verify_backup(
    bundle_path: Path,
    *,
    expected_manifest_sha256: str | None = None,
) -> BackupVerification:
    """Restore a bundle in isolation and validate every declared recovery identity."""

    bundle = Path(bundle_path)
    if bundle.is_symlink() or not bundle.is_dir():
        raise BackupVerificationError("backup bundle must be a regular directory")
    manifest_path = bundle / "manifest.json"
    manifest_sha256 = _sha256_file(manifest_path)
    if expected_manifest_sha256 is not None and manifest_sha256 != expected_manifest_sha256:
        raise BackupVerificationError("backup manifest SHA-256 does not match external receipt")
    manifest = _manifest(manifest_path)
    schema = manifest.get("schema_version")
    if schema == 1:
        return _verify_legacy_bundle(bundle, manifest, manifest_sha256=manifest_sha256)
    if schema == 2:
        return _verify_complete_bundle(bundle, manifest, manifest_sha256=manifest_sha256)
    if schema == 3:
        return _verify_v3_bundle(
            bundle,
            manifest,
            manifest_sha256=manifest_sha256,
            enforce_directory_name=True,
        )
    raise BackupVerificationError("unsupported backup manifest schema version")


def _verify_private_v3_staging(
    bundle: Path,
    *,
    expected_manifest_sha256: str,
    expected_backup_id: str,
) -> BackupVerification:
    manifest_path = bundle / "manifest.json"
    manifest_sha256 = _sha256_file(manifest_path)
    if manifest_sha256 != expected_manifest_sha256:
        raise BackupVerificationError("staged backup manifest SHA-256 changed")
    manifest = _manifest(manifest_path)
    if manifest.get("schema_version") != 3:
        raise BackupVerificationError("staged backup is not schema-v3")
    return _verify_v3_bundle(
        bundle,
        manifest,
        manifest_sha256=manifest_sha256,
        enforce_directory_name=False,
        expected_backup_id=expected_backup_id,
    )


def _validated_config_bytes(inputs: BackupBundleInputs) -> bytes:
    path = inputs.config_path
    if path.is_symlink() or not path.is_file():
        raise BackupError("production config must be a regular non-symlink file")
    try:
        rendered = path.read_bytes()
        validated = load_settings(path)
    except Exception as exc:
        raise BackupError("production config cannot be validated for backup") from exc
    observed_sha256 = hashlib.sha256(rendered).hexdigest()
    if observed_sha256 != inputs.config_sha256:
        raise BackupError("production config identity does not match deployment identity")
    if validated != inputs.settings:
        raise BackupError("production config validated settings changed before backup")
    forbidden = (b"ARM_MAC_KEY", b"WEBHOOK_TOKEN", b"password", b"access_token", b"secret_key")
    if any(token.lower() in rendered.lower() for token in forbidden):
        raise BackupError("validated production config unexpectedly contains secret material")
    return rendered


def _complete_members(
    temporary_bundle: Path,
    *,
    account_state_path: Path,
    inputs: BackupBundleInputs,
) -> tuple[dict[str, str], dict[str, object]]:
    account_destination = temporary_bundle / "account_state.json"
    _copy_fsynced(Path(account_state_path), account_destination, label="account state")
    _write_fsynced(temporary_bundle / "production_config.toml", _validated_config_bytes(inputs))
    copies = (
        (inputs.safety_manifest_path, "xtquant_safety_manifest.json", "XtQuant safety manifest"),
        (inputs.calendar_manifest_path, "trading_calendar.json", "trading calendar manifest"),
        (inputs.active_data_manifest_path, "active_data_source.json", "active data source manifest"),
        (
            inputs.strategy_data_manifest_path,
            "strategy_data_manifest.json",
            "strategy data manifest",
        ),
    )
    for source, name, label in copies:
        _copy_fsynced(source, temporary_bundle / name, label=label)
    member_hashes = {
        path.name: _sha256_file(path)
        for path in temporary_bundle.iterdir()
        if path.is_file() and path.name != "manifest.json"
    }
    deployment: dict[str, object] = {
        "schema": "firmquant.deployment-record.v1",
        "firmquant_commit": inputs.firmquant_commit,
        "uquant_commit": inputs.uquant_commit,
        "config_sha256": inputs.config_sha256,
        "account_sha256": inputs.account_sha256,
        "calendar_sha256": member_hashes["trading_calendar.json"],
        "active_data_manifest_sha256": member_hashes["active_data_source.json"],
        "strategy_data_manifest_sha256": member_hashes["strategy_data_manifest.json"],
        "decision_id": inputs.decision_id,
        "strategy_session": inputs.strategy_session.isoformat(),
    }
    deployment_path = temporary_bundle / "deployment_record.json"
    _write_fsynced(deployment_path, canonical_json(deployment).encode("utf-8"))
    member_hashes[deployment_path.name] = _sha256_file(deployment_path)
    return member_hashes, deployment


def _epoch_member(database: Database, *, kind: str, epoch: int) -> dict[str, object]:
    try:
        store = OperationalAuthorityStore(database)
        if kind == "account":
            account = store.active_account_epoch()
            if account.epoch != epoch:
                raise BackupError("active account authority epoch changed")
            return {
                "schema": "firmquant.account-authority-epoch-backup.v1",
                "epoch": account.epoch,
                "account_id_hash": account.account_id_hash,
                "account_state_sha256": account.account_state_sha256,
                "deployment_identity_sha256": account.deployment_identity_sha256,
                "payload": json.loads(account.payload_json),
                "payload_sha256": account.payload_sha256,
                "created_at": account.created_at.isoformat(),
            }
        if kind != "mode":
            raise ValueError("epoch member kind is invalid")
        mode = store.active_mode_epoch()
        if mode.epoch != epoch:
            raise BackupError("active mode epoch changed")
    except PersistenceConflict as exc:
        raise BackupError("typed operational authority epoch is invalid") from exc
    return {
        "schema": "firmquant.mode-epoch-backup.v1",
        "epoch": mode.epoch,
        "mode": mode.mode.value,
        "deployment_identity_sha256": mode.deployment_identity_sha256,
        "caps_sha256": mode.caps_sha256,
        "payload": json.loads(mode.payload_json),
        "payload_sha256": mode.payload_sha256,
        "created_at": mode.created_at.isoformat(),
    }


def _v3_members(
    database: Database,
    temporary_bundle: Path,
    *,
    account_state_path: Path,
    inputs: BackupBundleInputs,
) -> dict[str, str]:
    deployment = inputs.deployment_identity
    evidence = inputs.operational_evidence_identity
    if deployment is None or evidence is None or inputs.reason is None:
        raise BackupError("schema-v3 backup inputs are incomplete")
    if database.scalar("SELECT epoch FROM account_authority_active WHERE singleton_id=1") != (
        deployment.account_authority_epoch
    ):
        raise BackupError("schema-v3 account authority is not active")
    if database.scalar("SELECT epoch FROM mode_epoch_active WHERE singleton_id=1") != deployment.mode_epoch:
        raise BackupError("schema-v3 mode epoch is not active")
    observed_account = UquantAccountStateStore().hash_file(account_state_path)
    if observed_account != inputs.account_sha256:
        raise BackupError("schema-v3 AccountState differs from operational identity")
    try:
        _validate_data_manifests(
            inputs.active_data_manifest_path,
            inputs.strategy_data_manifest_path,
        )
    except BackupVerificationError as exc:
        raise BackupError("schema-v3 data manifest evidence is invalid") from exc
    account_epoch = _epoch_member(
        database,
        kind="account",
        epoch=deployment.account_authority_epoch,
    )
    mode_epoch = _epoch_member(database, kind="mode", epoch=deployment.mode_epoch)
    if (
        account_epoch["account_id_hash"] != deployment.account_id_hash
        or account_epoch["deployment_identity_sha256"] != deployment.sha256
        or mode_epoch["mode"] != deployment.mode.value
        or mode_epoch["deployment_identity_sha256"] != deployment.sha256
        or mode_epoch["caps_sha256"] != deployment.caps_sha256
    ):
        raise BackupError("schema-v3 authority epochs differ from deployment identity")
    account_destination = temporary_bundle / "account_state.json"
    _copy_verified_member(
        account_state_path,
        account_destination,
        expected_sha256=_sha256_file(account_state_path),
        label="account state",
    )
    _ensure_fsynced_content(
        temporary_bundle / "production_config.toml",
        _validated_config_bytes(inputs),
        label="production config",
    )
    for source, name, label in (
        (inputs.safety_manifest_path, "xtquant_safety_manifest.json", "XtQuant safety manifest"),
        (inputs.calendar_manifest_path, "trading_calendar.json", "trading calendar manifest"),
        (inputs.active_data_manifest_path, "active_data_source.json", "active data source manifest"),
        (
            inputs.strategy_data_manifest_path,
            "strategy_data_manifest.json",
            "strategy data manifest",
        ),
    ):
        _copy_verified_member(
            source,
            temporary_bundle / name,
            expected_sha256=_sha256_file(source),
            label=label,
        )
    _ensure_fsynced_content(
        temporary_bundle / "deployment_identity.json",
        deployment.canonical_json.encode("utf-8"),
        label="deployment identity",
    )
    _ensure_fsynced_content(
        temporary_bundle / "operational_evidence_identity.json",
        evidence.canonical_json.encode("utf-8"),
        label="operational evidence identity",
    )
    _ensure_fsynced_content(
        temporary_bundle / "account_authority_epoch.json",
        canonical_json(account_epoch).encode("utf-8"),
        label="account authority epoch",
    )
    _ensure_fsynced_content(
        temporary_bundle / "mode_epoch.json",
        canonical_json(mode_epoch).encode("utf-8"),
        label="mode epoch",
    )
    return {
        path.name: _sha256_file(path)
        for path in temporary_bundle.iterdir()
        if path.is_file() and path.name != "manifest.json"
    }


def _v3_backup_id_from_identities(
    reason: BackupReason,
    deployment: DeploymentIdentity,
    evidence: OperationalEvidenceIdentity,
) -> str:
    identity = canonical_json(
        {
            "schema": "firmquant.backup-operation-identity.v1",
            "reason": reason.value,
            "deployment_identity_sha256": deployment.sha256,
            "operational_evidence_identity_sha256": evidence.sha256,
            "account_authority_epoch": deployment.account_authority_epoch,
            "mode_epoch": deployment.mode_epoch,
            "broker_snapshot_id": evidence.broker_snapshot_id,
            "broker_snapshot_sha256": evidence.broker_snapshot_sha256,
            "strategy_session": evidence.strategy_session.isoformat(),
            "decision_id": evidence.decision_id,
        }
    )
    return "backup-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _v3_backup_id(inputs: BackupBundleInputs) -> str:
    deployment = inputs.deployment_identity
    evidence = inputs.operational_evidence_identity
    if deployment is None or evidence is None or inputs.reason is None:
        raise BackupError("schema-v3 backup inputs are incomplete")
    return _v3_backup_id_from_identities(inputs.reason, deployment, evidence)


def _publication_payload(
    *,
    backup_id: str,
    inputs: BackupBundleInputs,
    manifest_sha256: str,
    database_sha256: str,
) -> tuple[str, str]:
    deployment = cast(DeploymentIdentity, inputs.deployment_identity)
    evidence = cast(OperationalEvidenceIdentity, inputs.operational_evidence_identity)
    payload = canonical_json(
        {
            "schema": "firmquant.backup-publication-operation.v1",
            "backup_id": backup_id,
            "reason": cast(BackupReason, inputs.reason).value,
            "manifest_sha256": manifest_sha256,
            "database_sha256": database_sha256,
            "account_state_sha256": inputs.account_sha256,
            "deployment_identity_sha256": deployment.sha256,
            "operational_evidence_identity_sha256": evidence.sha256,
            "account_authority_epoch": deployment.account_authority_epoch,
            "mode_epoch": deployment.mode_epoch,
        }
    )
    return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _resume_v3_backup_publication(
    database: Database,
    *,
    bundle: Path,
    final_bundle: Path,
    inputs: BackupBundleInputs,
) -> BackupReceipt:
    if bundle == final_bundle:
        verification = verify_backup(bundle)
    else:
        manifest_sha256 = _sha256_file(bundle / "manifest.json")
        verification = _verify_private_v3_staging(
            bundle,
            expected_manifest_sha256=manifest_sha256,
            expected_backup_id=final_bundle.name,
        )
    deployment = cast(DeploymentIdentity, inputs.deployment_identity)
    evidence = cast(OperationalEvidenceIdentity, inputs.operational_evidence_identity)
    reason = cast(BackupReason, inputs.reason)
    if (
        verification.schema_version != 3
        or verification.reason is not reason
        or verification.deployment_identity_sha256 != deployment.sha256
        or verification.operational_evidence_identity_sha256 != evidence.sha256
        or verification.account_state_sha256 != inputs.account_sha256
    ):
        raise BackupError("existing schema-v3 backup staging identity conflicts")
    manifest = _manifest(bundle / "manifest.json")
    created_at_text = _canonical_utc_text(manifest["created_at"], label="creation time")
    operation_payload, operation_sha256 = _publication_payload(
        backup_id=verification.backup_id,
        inputs=inputs,
        manifest_sha256=verification.manifest_sha256,
        database_sha256=verification.database_sha256,
    )
    row = database.query_one(
        """
        SELECT operation_id,stage,reason,manifest_sha256,database_sha256,
               account_state_sha256,deployment_identity_sha256,
               operational_evidence_identity_sha256,account_authority_epoch,mode_epoch,
               payload_json,payload_sha256,bundle_name
        FROM backup_publication_operations WHERE backup_id=?
        """,
        (verification.backup_id,),
    )
    expected = (
        "backup-publication-" + operation_sha256,
        reason.value,
        verification.manifest_sha256,
        verification.database_sha256,
        inputs.account_sha256,
        deployment.sha256,
        evidence.sha256,
        deployment.account_authority_epoch,
        deployment.mode_epoch,
        operation_payload,
        operation_sha256,
    )
    now_text = datetime.now(UTC).isoformat()
    if row is None:
        with database.transaction():
            database.write(
                """
                INSERT INTO backup_publication_operations(
                    operation_id,backup_id,stage,reason,manifest_sha256,database_sha256,
                    account_state_sha256,deployment_identity_sha256,
                    operational_evidence_identity_sha256,account_authority_epoch,mode_epoch,
                    bundle_name,payload_json,payload_sha256,created_at,updated_at
                ) VALUES(?,?,'PREPARED',?,?,?,?,?,?,?, ?,NULL,?,?,?,?)
                """,
                (
                    expected[0],
                    verification.backup_id,
                    *expected[1:],
                    created_at_text,
                    created_at_text,
                ),
            )
        stage = "PREPARED"
    else:
        observed = (
            str(row["operation_id"]),
            str(row["reason"]),
            str(row["manifest_sha256"]),
            str(row["database_sha256"]),
            str(row["account_state_sha256"]),
            str(row["deployment_identity_sha256"]),
            str(row["operational_evidence_identity_sha256"]),
            int(row["account_authority_epoch"]),
            int(row["mode_epoch"]),
            str(row["payload_json"]),
            str(row["payload_sha256"]),
        )
        if observed != expected:
            if str(row["stage"]) in {"PREPARED", "PUBLISHED"}:
                with database.transaction():
                    database.write(
                        "UPDATE backup_publication_operations SET stage='CONTRADICTION',updated_at=? "
                        "WHERE backup_id=?",
                        (now_text, verification.backup_id),
                    )
            raise BackupError("schema-v3 backup publication identity collision")
        stage = str(row["stage"])
        if stage == "CONTRADICTION":
            raise BackupError("schema-v3 backup publication is contradictory")
    if bundle != final_bundle:
        if final_bundle.exists() or final_bundle.is_symlink():
            raise BackupError("schema-v3 final bundle collides with preserved evidence")
        _fsync_directory(bundle)
        _publish_directory(bundle, final_bundle)
    else:
        _fsync_directory(final_bundle.parent)
    if stage == "PREPARED":
        with database.transaction():
            database.write(
                "UPDATE backup_publication_operations SET stage='PUBLISHED',bundle_name=?,updated_at=? "
                "WHERE backup_id=?",
                (final_bundle.name, now_text, verification.backup_id),
            )
        stage = "PUBLISHED"
    with database.transaction():
        receipt_row = database.query_one(
            "SELECT manifest_sha256,database_sha256,bundle_schema_version FROM backup_receipts "
            "WHERE backup_id=?",
            (verification.backup_id,),
        )
        if receipt_row is None:
            if stage != "PUBLISHED":
                raise BackupError("schema-v3 publication receipt is missing after terminal stage")
            database.write(
                """
                INSERT INTO backup_receipts(
                    backup_id,database_sha256,account_state_sha256,manifest_json,
                    manifest_sha256,created_at,verified_at,verification_status,
                    bundle_schema_version,operational_schema_version,reason,
                    deployment_identity_sha256,operational_evidence_identity_sha256,
                    account_authority_epoch,mode_epoch,broker_snapshot_id,broker_snapshot_sha256
                ) VALUES(?,?,?,?,?,?,?,'VERIFIED',3,?,?,?,?,?,?,?,?)
                """,
                (
                    verification.backup_id,
                    verification.database_sha256,
                    inputs.account_sha256,
                    canonical_json(manifest),
                    verification.manifest_sha256,
                    created_at_text,
                    now_text,
                    verification.operational_schema_version,
                    reason.value,
                    deployment.sha256,
                    evidence.sha256,
                    deployment.account_authority_epoch,
                    deployment.mode_epoch,
                    evidence.broker_snapshot_id,
                    evidence.broker_snapshot_sha256,
                ),
            )
        elif tuple(receipt_row) != (
            verification.manifest_sha256,
            verification.database_sha256,
            3,
        ):
            raise BackupError("schema-v3 backup receipt collides with publication")
        if stage == "PUBLISHED":
            database.write(
                "UPDATE backup_publication_operations SET stage='RECEIPT_COMMITTED',updated_at=? "
                "WHERE backup_id=?",
                (now_text, verification.backup_id),
            )
    return BackupReceipt(
        backup_id=verification.backup_id,
        bundle_path=final_bundle,
        database_sha256=verification.database_sha256,
        account_state_sha256=verification.account_state_sha256,
        manifest_sha256=verification.manifest_sha256,
        audit_count=verification.audit_count,
        audit_head_hash=verification.audit_head_hash,
        created_at=created_at_text,
        schema_version=3,
        production_authority=True,
    )


def backup_state(
    database: Database,
    destination_directory: Path,
    *,
    account_state_path: Path | None = None,
    created_at: datetime | None = None,
    complete_inputs: BackupBundleInputs | None = None,
) -> BackupReceipt:
    """Create, verify, atomically publish, and receipt a legacy or complete state bundle."""

    root = Path(destination_directory)
    if root.is_symlink() or not root.is_dir():
        raise BackupError("backup destination must be a regular existing directory")
    timestamp = datetime.now(UTC) if created_at is None else created_at
    if not isinstance(timestamp, datetime) or timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise BackupError("backup created_at must be timezone-aware")
    if complete_inputs is not None and account_state_path is None:
        raise BackupError("complete backup requires uquant AccountState")
    utc_timestamp = timestamp.astimezone(UTC)
    is_v3 = complete_inputs is not None and complete_inputs.reason is not None
    backup_id = (
        _v3_backup_id(complete_inputs)
        if is_v3 and complete_inputs is not None
        else "backup-" + utc_timestamp.strftime("%Y%m%dT%H%M%S%fZ")
    )
    final_bundle = root / backup_id
    if final_bundle.exists() or final_bundle.is_symlink():
        if is_v3 and complete_inputs is not None and final_bundle.is_dir() and not final_bundle.is_symlink():
            return _resume_v3_backup_publication(
                database,
                bundle=final_bundle,
                final_bundle=final_bundle,
                inputs=complete_inputs,
            )
        raise BackupError("refusing to overwrite an existing backup bundle")
    if is_v3:
        temporary_bundle = root / f".{backup_id}.staging"
        if temporary_bundle.is_dir() and not temporary_bundle.is_symlink():
            if complete_inputs is None:
                raise BackupError("schema-v3 backup inputs are incomplete")
            if (temporary_bundle / "manifest.json").is_file():
                return _resume_v3_backup_publication(
                    database,
                    bundle=temporary_bundle,
                    final_bundle=final_bundle,
                    inputs=complete_inputs,
                )
            allowed_partial = _V3_MEMBERS | {f".{name}.copying" for name in _V3_MEMBERS | {"manifest.json"}}
            if {path.name for path in temporary_bundle.iterdir()} - allowed_partial:
                raise BackupError("schema-v3 backup staging collision requires preservation")
        else:
            try:
                temporary_bundle.mkdir(mode=0o700)
            except OSError as exc:
                raise BackupError("cannot create private backup staging directory") from exc
    else:
        temporary_bundle = Path(tempfile.mkdtemp(prefix=f".{backup_id}.", dir=root))
    if os.name != "nt":
        temporary_bundle.chmod(0o700)

    database_path = temporary_bundle / "firmquant.sqlite3"
    if database_path.exists() or database_path.is_symlink():
        if not is_v3 or database_path.is_symlink() or not database_path.is_file():
            raise BackupError("backup staging database collision")
        staged_database = Database.open_read_only(database_path, immutable=True)
        try:
            staged_database.integrity_check()
            if _database_schema_version(staged_database) != CURRENT_SCHEMA_VERSION:
                raise BackupError("backup partial staging schema is not current")
            _verify_migration_prefix(staged_database, CURRENT_SCHEMA_VERSION)
        finally:
            staged_database.close()
    else:
        database.backup_to(database_path)
    account_state_sha256: str | None = None
    account_manifest: dict[str, str] | None = None
    deployment: dict[str, object] | None = None
    complete_member_hashes: dict[str, str] | None = None
    if is_v3 and complete_inputs is not None:
        if account_state_path is None:
            raise BackupError("schema-v3 backup requires uquant AccountState")
        complete_member_hashes = _v3_members(
            database,
            temporary_bundle,
            account_state_path=Path(account_state_path),
            inputs=complete_inputs,
        )
        account_state_sha256 = complete_inputs.account_sha256
    elif complete_inputs is not None:
        if account_state_path is None:
            raise BackupError("complete backup requires uquant AccountState")
        complete_member_hashes, deployment = _complete_members(
            temporary_bundle,
            account_state_path=Path(account_state_path),
            inputs=complete_inputs,
        )
        account_state_sha256 = complete_member_hashes["account_state.json"]
    elif account_state_path is not None:
        account_destination = temporary_bundle / "account_state.json"
        _copy_fsynced(Path(account_state_path), account_destination, label="account state")
        account_state_sha256 = _sha256_file(account_destination)
        account_manifest = {
            "filename": "account_state.json",
            "sha256": account_state_sha256,
        }

    restored = Database.open_read_only(database_path, immutable=True)
    try:
        restored.integrity_check()
        audit = AuditLedger(restored).verify()
        schema_version = _database_schema_version(restored)
        if complete_inputs is not None and complete_inputs.decision_id is not None:
            decision = restored.query_one(
                "SELECT decision_id FROM decision_snapshots WHERE decision_id = ?",
                (complete_inputs.decision_id,),
            )
            if decision is None:
                raise BackupError("final backup snapshot does not contain the frozen decision")
    finally:
        restored.close()
    database_sha256 = _sha256_file(database_path)

    if complete_inputs is None:
        manifest_payload: dict[str, object] = {
            "schema_version": 1,
            "backup_id": backup_id,
            "created_at": utc_timestamp.isoformat(),
            "database": {"filename": "firmquant.sqlite3", "sha256": database_sha256},
            "account_state": account_manifest,
            "operational_schema_version": schema_version,
            "audit": {"count": audit.count, "head_hash": audit.head_hash},
        }
    elif not is_v3:
        if complete_member_hashes is None or deployment is None:
            raise BackupError("complete backup member identity was not prepared")
        complete_member_hashes["firmquant.sqlite3"] = database_sha256
        manifest_payload = {
            "schema_version": 2,
            "backup_id": backup_id,
            "created_at": utc_timestamp.isoformat(),
            "members": dict(sorted(complete_member_hashes.items())),
            "operational_schema_version": schema_version,
            "audit": {"count": audit.count, "head_hash": audit.head_hash},
            "deployment": deployment,
        }
    else:
        if complete_member_hashes is None or complete_inputs is None:
            raise BackupError("schema-v3 backup member identity was not prepared")
        deployment_identity = cast(DeploymentIdentity, complete_inputs.deployment_identity)
        evidence_identity = cast(
            OperationalEvidenceIdentity,
            complete_inputs.operational_evidence_identity,
        )
        complete_member_hashes["firmquant.sqlite3"] = database_sha256
        manifest_payload = {
            "schema_version": 3,
            "backup_id": backup_id,
            "created_at": utc_timestamp.isoformat(),
            "reason": cast(BackupReason, complete_inputs.reason).value,
            "members": dict(sorted(complete_member_hashes.items())),
            "operational_schema_version": schema_version,
            "audit": {"count": audit.count, "head_hash": audit.head_hash},
            "deployment_identity_sha256": deployment_identity.sha256,
            "operational_evidence_identity_sha256": evidence_identity.sha256,
            "account_state_sha256": complete_inputs.account_sha256,
            "account_authority_epoch": deployment_identity.account_authority_epoch,
            "mode_epoch": deployment_identity.mode_epoch,
            "broker_snapshot_id": evidence_identity.broker_snapshot_id,
            "broker_snapshot_sha256": evidence_identity.broker_snapshot_sha256,
            "broker_event_watermark": evidence_identity.broker_event_watermark,
            "strategy_session": evidence_identity.strategy_session.isoformat(),
            "decision_id": complete_inputs.decision_id,
        }

    manifest_bytes = canonical_json(manifest_payload).encode("utf-8")
    manifest_path = temporary_bundle / "manifest.json"
    if is_v3:
        _ensure_fsynced_content(manifest_path, manifest_bytes, label="backup manifest")
    else:
        _write_fsynced(manifest_path, manifest_bytes)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if is_v3:
        _verify_private_v3_staging(
            temporary_bundle,
            expected_manifest_sha256=manifest_sha256,
            expected_backup_id=backup_id,
        )
    else:
        verify_backup(temporary_bundle, expected_manifest_sha256=manifest_sha256)

    if is_v3 and complete_inputs is not None:
        deployment_identity = cast(DeploymentIdentity, complete_inputs.deployment_identity)
        evidence_identity = cast(
            OperationalEvidenceIdentity,
            complete_inputs.operational_evidence_identity,
        )
        operation_payload, operation_payload_sha256 = _publication_payload(
            backup_id=backup_id,
            inputs=complete_inputs,
            manifest_sha256=manifest_sha256,
            database_sha256=database_sha256,
        )
        with database.transaction():
            database.write(
                """
                INSERT INTO backup_publication_operations(
                    operation_id,backup_id,stage,reason,manifest_sha256,database_sha256,
                    account_state_sha256,deployment_identity_sha256,
                    operational_evidence_identity_sha256,account_authority_epoch,mode_epoch,
                    bundle_name,payload_json,payload_sha256,created_at,updated_at
                ) VALUES(?,?,'PREPARED',?,?,?,?,?,?,?, ?,NULL,?,?,?,?)
                """,
                (
                    "backup-publication-" + operation_payload_sha256,
                    backup_id,
                    cast(BackupReason, complete_inputs.reason).value,
                    manifest_sha256,
                    database_sha256,
                    complete_inputs.account_sha256,
                    deployment_identity.sha256,
                    evidence_identity.sha256,
                    deployment_identity.account_authority_epoch,
                    deployment_identity.mode_epoch,
                    operation_payload,
                    operation_payload_sha256,
                    utc_timestamp.isoformat(),
                    utc_timestamp.isoformat(),
                ),
            )

    _fsync_directory(temporary_bundle)
    _publish_directory(temporary_bundle, final_bundle)

    if is_v3 and complete_inputs is not None:
        with database.transaction():
            database.write(
                """
                UPDATE backup_publication_operations
                SET stage='PUBLISHED',bundle_name=?,updated_at=? WHERE backup_id=?
                """,
                (final_bundle.name, datetime.now(UTC).isoformat(), backup_id),
            )
        with database.transaction():
            deployment_identity = cast(DeploymentIdentity, complete_inputs.deployment_identity)
            evidence_identity = cast(
                OperationalEvidenceIdentity,
                complete_inputs.operational_evidence_identity,
            )
            database.write(
                """
                INSERT INTO backup_receipts(
                    backup_id,database_sha256,account_state_sha256,manifest_json,
                    manifest_sha256,created_at,verified_at,verification_status,
                    bundle_schema_version,operational_schema_version,reason,
                    deployment_identity_sha256,operational_evidence_identity_sha256,
                    account_authority_epoch,mode_epoch,broker_snapshot_id,broker_snapshot_sha256
                ) VALUES(?,?,?,?,?,?,?,'VERIFIED',3,?,?,?,?,?,?,?,?)
                """,
                (
                    backup_id,
                    database_sha256,
                    complete_inputs.account_sha256,
                    canonical_json(manifest_payload),
                    manifest_sha256,
                    utc_timestamp.isoformat(),
                    datetime.now(UTC).isoformat(),
                    schema_version,
                    cast(BackupReason, complete_inputs.reason).value,
                    deployment_identity.sha256,
                    evidence_identity.sha256,
                    deployment_identity.account_authority_epoch,
                    deployment_identity.mode_epoch,
                    evidence_identity.broker_snapshot_id,
                    evidence_identity.broker_snapshot_sha256,
                ),
            )
            database.write(
                """
                UPDATE backup_publication_operations
                SET stage='RECEIPT_COMMITTED',updated_at=? WHERE backup_id=?
                """,
                (datetime.now(UTC).isoformat(), backup_id),
            )
    else:
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
        schema_version=3 if is_v3 else (2 if complete_inputs is not None else 1),
        production_authority=is_v3,
    )


def _is_reparse(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


@dataclass(frozen=True, slots=True)
class _ParentIdentity:
    resolved: Path
    device: int
    inode: int


def _canonical_no_link_path(path: Path, *, label: str, must_exist: bool) -> Path:
    lexical = Path(path)
    if ".." in lexical.parts:
        raise BackupVerificationError(f"restore {label} contains a lexical alias")
    try:
        absolute = lexical.absolute()
        current = absolute
        while True:
            if (current.exists() or current.is_symlink()) and (current.is_symlink() or _is_reparse(current)):
                raise BackupVerificationError(f"restore {label} has a symlink or reparse ancestor")
            parent = current.parent
            if parent == current:
                break
            current = parent
        return absolute.resolve(strict=must_exist)
    except BackupVerificationError:
        raise
    except OSError as exc:
        raise BackupVerificationError(f"restore {label} cannot be resolved") from exc


def _parent_identity(destination: Path) -> _ParentIdentity:
    try:
        parent = destination.parent.resolve(strict=True)
        identity = parent.stat(follow_symlinks=False)
    except OSError as exc:
        raise BackupVerificationError("restore destination parent identity is unavailable") from exc
    if parent.is_symlink() or _is_reparse(parent):
        raise BackupVerificationError("restore destination parent is a symlink or reparse point")
    return _ParentIdentity(parent, identity.st_dev, identity.st_ino)


def _revalidate_parent(destination: Path, expected: _ParentIdentity) -> None:
    observed = _parent_identity(destination)
    if observed != expected:
        raise BackupVerificationError("restore destination parent identity changed before publication")


def _destination_identity(destination: Path) -> str:
    canonical = _canonical_no_link_path(destination, label="destination", must_exist=False)
    return hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()


def _validate_restore_paths(bundle: Path, destination: Path) -> tuple[Path, Path, _ParentIdentity]:
    source_absolute = _canonical_no_link_path(bundle, label="source", must_exist=True)
    destination_absolute = _canonical_no_link_path(
        destination,
        label="destination",
        must_exist=destination.exists(),
    )
    if (
        source_absolute == destination_absolute
        or source_absolute in destination_absolute.parents
        or destination_absolute in source_absolute.parents
    ):
        raise BackupVerificationError("restore source and destination must not contain each other")
    if destination.exists():
        if not destination.is_dir():
            raise BackupVerificationError("restore destination must be a directory")
    elif not destination.parent.is_dir():
        raise BackupVerificationError("restore destination parent must exist")
    return source_absolute, destination_absolute, _parent_identity(destination_absolute)


def _logical_value(value: object) -> object:
    if isinstance(value, bytes):
        return {"blob_sha256": hashlib.sha256(value).hexdigest(), "length": len(value)}
    if value is None or isinstance(value, (str, int, float)):
        return value
    raise BackupVerificationError("restored logical state contains unsupported SQLite value")


def _logical_state_sha256(database: Database) -> str:
    excluded = {"restore_operations", "restore_receipts", "sqlite_sequence"}
    names = tuple(
        str(row["name"])
        for row in database.query_all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        if str(row["name"]) not in excluded
    )
    tables: list[dict[str, object]] = []
    for name in names:
        if not name.replace("_", "").isalnum():
            raise BackupVerificationError("restored logical state has an unsafe table name")
        columns = tuple(str(row["name"]) for row in database.query_all(f"PRAGMA table_info({name})"))
        rows = [
            [_logical_value(row[column]) for column in columns]
            for row in database.query_all(f"SELECT * FROM {name}")
        ]
        rows.sort(key=canonical_json)
        tables.append({"name": name, "columns": list(columns), "rows": rows})
    payload = canonical_json({"schema": "firmquant.sanitized-logical-state.v1", "tables": tables})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _restore_identity(verification: BackupVerification, destination: Path) -> tuple[str, str]:
    destination_sha256 = _destination_identity(destination)
    payload = canonical_json(
        {
            "schema": "firmquant.restore-identity.v1",
            "source_backup_id": verification.backup_id,
            "source_manifest_sha256": verification.manifest_sha256,
            "source_database_sha256": verification.database_sha256,
            "destination_identity_sha256": destination_sha256,
        }
    )
    return "restore-" + hashlib.sha256(payload.encode("utf-8")).hexdigest(), destination_sha256


def _source_bundle_members(
    bundle: Path,
    *,
    verification: BackupVerification,
) -> dict[str, str]:
    manifest = _manifest(bundle / "manifest.json")
    members = _mapping(manifest.get("members"), label="members")
    if set(members) != _V3_MEMBERS:
        raise BackupVerificationError("restore source member contract changed")
    result = {
        name: _lower_digest(value, label=f"restore source member {name}") for name, value in members.items()
    }
    result["manifest.json"] = verification.manifest_sha256
    return result


def _copy_source_bundle_to_staging(
    bundle: Path,
    staging: Path,
    *,
    verification: BackupVerification,
) -> BackupVerification:
    expected = _source_bundle_members(bundle, verification=verification)
    allowed = set(expected) | {f".{name}.copying" for name in expected}
    try:
        observed_names = {path.name for path in staging.iterdir()}
    except OSError as exc:
        raise BackupVerificationError("restore staging cannot be inspected") from exc
    if not observed_names <= allowed:
        raise BackupVerificationError("restore staging collision requires manual preservation")
    for name in sorted(expected):
        _copy_verified_member(
            bundle / name,
            staging / name,
            expected_sha256=expected[name],
            label=f"backup member {name}",
        )
    staged = _verify_private_v3_staging(
        staging,
        expected_manifest_sha256=verification.manifest_sha256,
        expected_backup_id=verification.backup_id,
    )
    if staged != verification:
        raise BackupVerificationError("copied restore source facts changed before mutation")
    return staged


def _ensure_source_backup_receipt(
    database: Database,
    *,
    verification: BackupVerification,
    manifest: Mapping[str, object],
    now: datetime,
) -> None:
    existing = database.query_one(
        "SELECT manifest_sha256,database_sha256,verification_status,bundle_schema_version "
        "FROM backup_receipts WHERE backup_id=?",
        (verification.backup_id,),
    )
    if existing is not None:
        if tuple(existing) != (
            verification.manifest_sha256,
            verification.database_sha256,
            "VERIFIED",
            3,
        ):
            raise BackupVerificationError("restored source backup receipt collides with existing evidence")
        return
    if (
        verification.reason is None
        or verification.deployment_identity_sha256 is None
        or verification.operational_evidence_identity_sha256 is None
        or verification.account_authority_epoch is None
        or verification.mode_epoch is None
        or verification.broker_snapshot_id is None
        or verification.broker_snapshot_sha256 is None
        or verification.account_state_sha256 is None
    ):
        raise BackupVerificationError("verified schema-v3 source identity is incomplete")
    operation_payload = canonical_json(
        {
            "schema": "firmquant.backup-publication-operation.v1",
            "backup_id": verification.backup_id,
            "reason": verification.reason.value,
            "manifest_sha256": verification.manifest_sha256,
            "database_sha256": verification.database_sha256,
            "account_state_sha256": verification.account_state_sha256,
            "deployment_identity_sha256": verification.deployment_identity_sha256,
            "operational_evidence_identity_sha256": (verification.operational_evidence_identity_sha256),
            "account_authority_epoch": verification.account_authority_epoch,
            "mode_epoch": verification.mode_epoch,
        }
    )
    operation_sha256 = hashlib.sha256(operation_payload.encode("utf-8")).hexdigest()
    timestamp = now.isoformat()
    database.write(
        """
        INSERT INTO backup_publication_operations(
            operation_id,backup_id,stage,reason,manifest_sha256,database_sha256,
            account_state_sha256,deployment_identity_sha256,
            operational_evidence_identity_sha256,account_authority_epoch,mode_epoch,
            bundle_name,payload_json,payload_sha256,created_at,updated_at
        ) VALUES(?,?,'PREPARED',?,?,?,?,?,?,?, ?,NULL,?,?,?,?)
        """,
        (
            "backup-publication-" + operation_sha256,
            verification.backup_id,
            verification.reason.value,
            verification.manifest_sha256,
            verification.database_sha256,
            verification.account_state_sha256,
            verification.deployment_identity_sha256,
            verification.operational_evidence_identity_sha256,
            verification.account_authority_epoch,
            verification.mode_epoch,
            operation_payload,
            operation_sha256,
            timestamp,
            timestamp,
        ),
    )
    database.write(
        "UPDATE backup_publication_operations SET stage='PUBLISHED',bundle_name=?,updated_at=? "
        "WHERE backup_id=?",
        (verification.backup_id, timestamp, verification.backup_id),
    )
    database.write(
        """
        INSERT INTO backup_receipts(
            backup_id,database_sha256,account_state_sha256,manifest_json,
            manifest_sha256,created_at,verified_at,verification_status,
            bundle_schema_version,operational_schema_version,reason,
            deployment_identity_sha256,operational_evidence_identity_sha256,
            account_authority_epoch,mode_epoch,broker_snapshot_id,broker_snapshot_sha256
        ) VALUES(?,?,?,?,?,?,?,'VERIFIED',3,?,?,?,?,?,?,?,?)
        """,
        (
            verification.backup_id,
            verification.database_sha256,
            verification.account_state_sha256,
            canonical_json(dict(manifest)),
            verification.manifest_sha256,
            cast(str, manifest["created_at"]),
            timestamp,
            verification.operational_schema_version,
            verification.reason.value,
            verification.deployment_identity_sha256,
            verification.operational_evidence_identity_sha256,
            verification.account_authority_epoch,
            verification.mode_epoch,
            verification.broker_snapshot_id,
            verification.broker_snapshot_sha256,
        ),
    )
    database.write(
        "UPDATE backup_publication_operations SET stage='RECEIPT_COMMITTED',updated_at=? WHERE backup_id=?",
        (timestamp, verification.backup_id),
    )


def _restore_receipt_from_row(row: sqlite3.Row, destination: Path) -> RestoreReceipt:
    return RestoreReceipt(
        restore_id=str(row["restore_id"]),
        source_backup_id=str(row["source_backup_id"]),
        destination=destination,
        source_manifest_sha256=str(row["source_manifest_sha256"]),
        sanitized_state_sha256=str(row["sanitized_state_sha256"]),
        original_audit_count=cast(int, row["original_audit_count"]),
        original_audit_head=str(row["original_audit_head"]),
        restored_audit_count=cast(int, row["restored_audit_count"]),
        restored_audit_head=str(row["restored_audit_head"]),
        restored_at=str(row["restored_at"]),
    )


def _probe_existing_restore(
    destination: Path,
    *,
    verification: BackupVerification,
    restore_id: str,
    destination_sha256: str,
) -> tuple[str, str, RestoreReceipt | None]:
    try:
        if {path.name for path in destination.iterdir()} != _V3_MEMBERS | {"manifest.json"}:
            raise BackupVerificationError("restore destination directory entries are not exact")
    except OSError as exc:
        raise BackupVerificationError("restore destination cannot be inspected immutably") from exc
    database_path = destination / "firmquant.sqlite3"
    if database_path.is_symlink() or not database_path.is_file():
        raise BackupVerificationError("restore destination is not exact recoverable evidence")
    try:
        database = Database.open_read_only(database_path, immutable=True)
        try:
            if _database_schema_version(database) != CURRENT_SCHEMA_VERSION:
                raise BackupVerificationError("restore destination schema is not current")
            _verify_migration_prefix(database, CURRENT_SCHEMA_VERSION)
            operation = database.query_one(
                """
                SELECT stage,backup_id,source_reason,source_manifest_sha256,
                       source_database_sha256,destination_identity_sha256,
                       sanitized_state_sha256,final_directory_name,deployment_identity_sha256,
                       operational_evidence_identity_sha256,account_authority_epoch,mode_epoch
                FROM restore_operations WHERE restore_id=?
                """,
                (restore_id,),
            )
            if operation is None or operation["sanitized_state_sha256"] is None:
                raise BackupVerificationError("restore destination operation proof is missing")
            stage = str(operation["stage"])
            observed = (
                str(operation["backup_id"]),
                str(operation["source_reason"]),
                str(operation["source_manifest_sha256"]),
                str(operation["source_database_sha256"]),
                str(operation["destination_identity_sha256"]),
                str(operation["deployment_identity_sha256"]),
                str(operation["operational_evidence_identity_sha256"]),
                int(operation["account_authority_epoch"]),
                int(operation["mode_epoch"]),
            )
            expected = (
                verification.backup_id,
                cast(BackupReason, verification.reason).value,
                verification.manifest_sha256,
                verification.database_sha256,
                destination_sha256,
                verification.deployment_identity_sha256,
                verification.operational_evidence_identity_sha256,
                verification.account_authority_epoch,
                verification.mode_epoch,
            )
            if stage not in {"STAGED", "PUBLISHED", "RECEIPT_COMMITTED"} or observed != expected:
                raise BackupVerificationError("restore destination operation proof conflicts")
            final_name = operation["final_directory_name"]
            if (stage == "STAGED" and final_name is not None) or (
                stage != "STAGED" and final_name != destination.name
            ):
                raise BackupVerificationError("restore destination publication name conflicts")
            sanitized_state_sha256 = str(operation["sanitized_state_sha256"])
            _verify_sanitized_restore(
                database,
                restore_id=restore_id,
                expected_sha256=sanitized_state_sha256,
            )
            receipt_row = database.query_one(
                "SELECT * FROM restore_receipts WHERE restore_id=?", (restore_id,)
            )
            receipt = None
            if receipt_row is not None:
                receipt = _restore_receipt_from_row(receipt_row, destination)
                if (
                    receipt.source_backup_id != verification.backup_id
                    or receipt.source_manifest_sha256 != verification.manifest_sha256
                    or receipt.sanitized_state_sha256 != sanitized_state_sha256
                    or stage != "RECEIPT_COMMITTED"
                ):
                    raise BackupVerificationError("restore destination receipt proof conflicts")
            elif stage == "RECEIPT_COMMITTED":
                raise BackupVerificationError("restore terminal operation lacks its receipt")
            return stage, sanitized_state_sha256, receipt
        finally:
            database.close()
    except PersistenceError as exc:
        raise BackupVerificationError("restore destination immutable probe failed") from exc


def _verify_sanitized_restore(
    database: Database,
    *,
    restore_id: str,
    expected_sha256: str,
) -> tuple[int, str]:
    database.integrity_check()
    if database.query_all("PRAGMA foreign_key_check"):
        raise BackupVerificationError("restored database foreign keys are invalid")
    if database.scalar("SELECT state FROM runtime_state WHERE singleton_id=1") != "DISARMED":
        raise BackupVerificationError("restored runtime is not DISARMED")
    if database.scalar("SELECT count(*) FROM arm_leases WHERE revoked_at IS NULL") != 0:
        raise BackupVerificationError("restored database retained active arm authority")
    if database.scalar("SELECT count(*) FROM writer_leases") != 0:
        raise BackupVerificationError("restored database retained writer authority")
    if database.scalar("SELECT count(*) FROM production_heartbeat") != 0:
        raise BackupVerificationError("restored database retained heartbeat authority")
    observed_sha256 = _logical_state_sha256(database)
    if observed_sha256 != expected_sha256:
        raise BackupVerificationError("restored sanitized logical state digest changed")
    audit = AuditLedger(database).verify()
    operation = database.query_one(
        "SELECT restore_id FROM restore_operations WHERE restore_id=?",
        (restore_id,),
    )
    if operation is None:
        raise BackupVerificationError("restored operation evidence is missing")
    return audit.count, audit.head_hash


def _finalize_restore(
    destination: Path,
    *,
    verification: BackupVerification,
    restore_id: str,
    sanitized_state_sha256: str,
    restored_at: datetime,
) -> RestoreReceipt:
    database = Database.open(destination / "firmquant.sqlite3")
    try:
        existing = database.query_one("SELECT * FROM restore_receipts WHERE restore_id=?", (restore_id,))
        if existing is not None:
            _verify_sanitized_restore(
                database,
                restore_id=restore_id,
                expected_sha256=sanitized_state_sha256,
            )
            return _restore_receipt_from_row(existing, destination)
        operation = database.query_one(
            "SELECT stage FROM restore_operations WHERE restore_id=?", (restore_id,)
        )
        if operation is None or str(operation["stage"]) not in {"STAGED", "PUBLISHED"}:
            raise BackupVerificationError("restore publication operation cannot be resumed")
        restored_count, restored_head = _verify_sanitized_restore(
            database,
            restore_id=restore_id,
            expected_sha256=sanitized_state_sha256,
        )
        payload = canonical_json(
            {
                "schema": "firmquant.restore-receipt.v1",
                "restore_id": restore_id,
                "source_backup_id": verification.backup_id,
                "source_manifest_sha256": verification.manifest_sha256,
                "source_reason": cast(BackupReason, verification.reason).value,
                "deployment_identity_sha256": verification.deployment_identity_sha256,
                "operational_evidence_identity_sha256": (verification.operational_evidence_identity_sha256),
                "account_authority_epoch": verification.account_authority_epoch,
                "mode_epoch": verification.mode_epoch,
                "source_database_sha256": verification.database_sha256,
                "sanitized_state_sha256": sanitized_state_sha256,
                "original_audit_count": verification.audit_count,
                "original_audit_head": verification.audit_head_hash,
                "restored_audit_count": restored_count,
                "restored_audit_head": restored_head,
                "restored_at": restored_at.isoformat(),
                "requires_fresh_snapshot": True,
                "requires_reconciliation": True,
            }
        )
        payload_sha256 = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if str(operation["stage"]) == "STAGED":
            with database.transaction():
                database.write(
                    "UPDATE restore_operations SET stage='PUBLISHED',final_directory_name=?,updated_at=? "
                    "WHERE restore_id=?",
                    (destination.name, restored_at.isoformat(), restore_id),
                )
        with database.transaction():
            database.write(
                """
                INSERT INTO restore_receipts(
                    restore_id,source_backup_id,source_manifest_sha256,source_reason,
                    deployment_identity_sha256,operational_evidence_identity_sha256,
                    account_authority_epoch,mode_epoch,source_database_sha256,sanitized_state_sha256,
                    original_audit_count,original_audit_head,restored_audit_count,restored_audit_head,
                    restored_at,requires_fresh_snapshot,requires_reconciliation,payload_json,payload_sha256
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,1,?,?)
                """,
                (
                    restore_id,
                    verification.backup_id,
                    verification.manifest_sha256,
                    cast(BackupReason, verification.reason).value,
                    verification.deployment_identity_sha256,
                    verification.operational_evidence_identity_sha256,
                    verification.account_authority_epoch,
                    verification.mode_epoch,
                    verification.database_sha256,
                    sanitized_state_sha256,
                    verification.audit_count,
                    verification.audit_head_hash,
                    restored_count,
                    restored_head,
                    restored_at.isoformat(),
                    payload,
                    payload_sha256,
                ),
            )
            database.write(
                "UPDATE restore_operations SET stage='RECEIPT_COMMITTED',updated_at=? WHERE restore_id=?",
                (restored_at.isoformat(), restore_id),
            )
        row = database.query_one("SELECT * FROM restore_receipts WHERE restore_id=?", (restore_id,))
        if row is None:
            raise BackupVerificationError("restore receipt publication failed")
        return _restore_receipt_from_row(row, destination)
    finally:
        database.close()


def _inspect_staging_operation(
    staging: Path,
    *,
    verification: BackupVerification,
    restore_id: str,
    destination_sha256: str,
) -> tuple[str, str | None] | None:
    database_path = staging / "firmquant.sqlite3"
    if not database_path.exists() and not database_path.is_symlink():
        return None
    if database_path.is_symlink() or not database_path.is_file():
        raise BackupVerificationError("restore staging database is not regular evidence")
    try:
        database = Database.open_read_only(database_path, immutable=True)
        try:
            if _database_schema_version(database) != CURRENT_SCHEMA_VERSION:
                raise BackupVerificationError("restore staging schema is not current")
            _verify_migration_prefix(database, CURRENT_SCHEMA_VERSION)
            operation = database.query_one(
                """
                SELECT stage,backup_id,source_reason,source_manifest_sha256,
                       source_database_sha256,destination_identity_sha256,
                       sanitized_state_sha256,deployment_identity_sha256,
                       operational_evidence_identity_sha256,account_authority_epoch,mode_epoch
                FROM restore_operations WHERE restore_id=?
                """,
                (restore_id,),
            )
            if operation is None:
                return None
            stage = str(operation["stage"])
            expected = (
                verification.backup_id,
                cast(BackupReason, verification.reason).value,
                verification.manifest_sha256,
                verification.database_sha256,
                destination_sha256,
                verification.deployment_identity_sha256,
                verification.operational_evidence_identity_sha256,
                verification.account_authority_epoch,
                verification.mode_epoch,
            )
            observed = (
                str(operation["backup_id"]),
                str(operation["source_reason"]),
                str(operation["source_manifest_sha256"]),
                str(operation["source_database_sha256"]),
                str(operation["destination_identity_sha256"]),
                str(operation["deployment_identity_sha256"]),
                str(operation["operational_evidence_identity_sha256"]),
                int(operation["account_authority_epoch"]),
                int(operation["mode_epoch"]),
            )
            if stage not in {"PREPARED", "STAGED"} or observed != expected:
                raise BackupVerificationError("restore staging operation proof conflicts")
            sanitized = operation["sanitized_state_sha256"]
            if stage == "STAGED":
                if sanitized is None:
                    raise BackupVerificationError("restore STAGED proof lacks logical state")
                _verify_sanitized_restore(
                    database,
                    restore_id=restore_id,
                    expected_sha256=str(sanitized),
                )
            elif sanitized is not None:
                raise BackupVerificationError("restore PREPARED proof has premature logical state")
            return stage, None if sanitized is None else str(sanitized)
        finally:
            database.close()
    except PersistenceError as exc:
        raise BackupVerificationError("restore staging collision requires manual preservation") from exc


def _prepare_staged_restore(
    staging: Path,
    *,
    verification: BackupVerification,
    restore_id: str,
    destination_sha256: str,
    restored_at: datetime,
) -> str:
    manifest = _manifest(staging / "manifest.json")
    database = Database.open(staging / "firmquant.sqlite3")
    try:
        operation_payload = canonical_json(
            {
                "schema": "firmquant.restore-operation.v1",
                "restore_id": restore_id,
                "source_backup_id": verification.backup_id,
                "source_manifest_sha256": verification.manifest_sha256,
                "source_database_sha256": verification.database_sha256,
                "destination_identity_sha256": destination_sha256,
                "deployment_identity_sha256": verification.deployment_identity_sha256,
                "operational_evidence_identity_sha256": (verification.operational_evidence_identity_sha256),
                "account_authority_epoch": verification.account_authority_epoch,
                "mode_epoch": verification.mode_epoch,
            }
        )
        operation_sha256 = hashlib.sha256(operation_payload.encode("utf-8")).hexdigest()
        with database.transaction():
            _ensure_source_backup_receipt(
                database,
                verification=verification,
                manifest=manifest,
                now=restored_at,
            )
            runtime = database.query_one("SELECT revision FROM runtime_state WHERE singleton_id=1")
            if runtime is None:
                raise BackupVerificationError("schema-v3 source lacks runtime state")
            database.write(
                """
                UPDATE runtime_state
                SET state='DISARMED',revision=?,reason='RESTORE_REQUIRES_RECONCILIATION',
                    blockers_json='["FRESH_SNAPSHOT_REQUIRED","RECONCILIATION_REQUIRED"]',
                    updated_at=? WHERE singleton_id=1
                """,
                (int(runtime["revision"]) + 1, restored_at.isoformat()),
            )
            database.write(
                "UPDATE arm_leases SET revoked_at=?,revoke_reason='RESTORE_REVOKED' WHERE revoked_at IS NULL",
                (restored_at.isoformat(),),
            )
            database.write("DELETE FROM writer_leases")
            database.write("DELETE FROM production_heartbeat")
            AuditLedger(database).append(
                audit_event_id="restore:" + restore_id.removeprefix("restore-"),
                category="RESTORE",
                actor="operator",
                payload={
                    "source_backup_hash": hashlib.sha256(verification.backup_id.encode("utf-8")).hexdigest(),
                    "source_manifest_sha256": verification.manifest_sha256,
                    "destination_identity_sha256": destination_sha256,
                    "runtime_state": "DISARMED",
                    "requires_fresh_snapshot": True,
                    "requires_reconciliation": True,
                },
                created_at=restored_at,
            )
            database.write(
                """
                INSERT INTO restore_operations(
                    operation_id,restore_id,backup_id,stage,source_reason,
                    source_manifest_sha256,source_database_sha256,destination_identity_sha256,
                    sanitized_state_sha256,final_directory_name,deployment_identity_sha256,
                    operational_evidence_identity_sha256,account_authority_epoch,mode_epoch,
                    payload_json,payload_sha256,created_at,updated_at
                ) VALUES(?,?,?,'PREPARED',?,?,?,?,NULL,NULL,?,?,?,?,?,?,?,?)
                """,
                (
                    "restore-operation-" + operation_sha256,
                    restore_id,
                    verification.backup_id,
                    cast(BackupReason, verification.reason).value,
                    verification.manifest_sha256,
                    verification.database_sha256,
                    destination_sha256,
                    verification.deployment_identity_sha256,
                    verification.operational_evidence_identity_sha256,
                    verification.account_authority_epoch,
                    verification.mode_epoch,
                    operation_payload,
                    operation_sha256,
                    restored_at.isoformat(),
                    restored_at.isoformat(),
                ),
            )
        sanitized_state_sha256 = _logical_state_sha256(database)
        with database.transaction():
            database.write(
                "UPDATE restore_operations SET stage='STAGED',sanitized_state_sha256=?,updated_at=? "
                "WHERE restore_id=?",
                (sanitized_state_sha256, restored_at.isoformat(), restore_id),
            )
        _verify_sanitized_restore(
            database,
            restore_id=restore_id,
            expected_sha256=sanitized_state_sha256,
        )
        return sanitized_state_sha256
    finally:
        database.close()


def _complete_prepared_restore(
    staging: Path,
    *,
    restore_id: str,
    restored_at: datetime,
) -> str:
    database = Database.open(staging / "firmquant.sqlite3")
    try:
        sanitized_state_sha256 = _logical_state_sha256(database)
        with database.transaction():
            database.write(
                "UPDATE restore_operations SET stage='STAGED',sanitized_state_sha256=?,updated_at=? "
                "WHERE restore_id=? AND stage='PREPARED'",
                (sanitized_state_sha256, restored_at.isoformat(), restore_id),
            )
        _verify_sanitized_restore(
            database,
            restore_id=restore_id,
            expected_sha256=sanitized_state_sha256,
        )
        return sanitized_state_sha256
    finally:
        database.close()


def _resume_staged_restore(
    staging: Path,
    destination: Path,
    *,
    bundle: Path,
    verification: BackupVerification,
    restore_id: str,
    destination_sha256: str,
    restored_at: datetime,
    destination_parent_identity: _ParentIdentity,
) -> RestoreReceipt:
    if not staging.is_dir() or staging.is_symlink():
        raise BackupVerificationError("restore staging collision requires manual preservation")
    operation = _inspect_staging_operation(
        staging,
        verification=verification,
        restore_id=restore_id,
        destination_sha256=destination_sha256,
    )
    if operation is None:
        _copy_source_bundle_to_staging(
            bundle=bundle,
            staging=staging,
            verification=verification,
        )
        sanitized_state_sha256 = _prepare_staged_restore(
            staging,
            verification=verification,
            restore_id=restore_id,
            destination_sha256=destination_sha256,
            restored_at=restored_at,
        )
    elif operation[0] == "PREPARED":
        sanitized_state_sha256 = _complete_prepared_restore(
            staging,
            restore_id=restore_id,
            restored_at=restored_at,
        )
    else:
        sanitized_state_sha256 = cast(str, operation[1])
    try:
        for member in staging.iterdir():
            if member.is_file():
                with member.open("r+b") as stream:
                    os.fsync(stream.fileno())
        _fsync_directory(staging)
        _revalidate_parent(destination, destination_parent_identity)
        if destination.exists():
            destination.rmdir()
        _revalidate_parent(destination, destination_parent_identity)
        _publish_directory(staging, destination)
    except OSError as exc:
        raise BackupError("cannot publish staged restored backup") from exc
    return _finalize_restore(
        destination,
        verification=verification,
        restore_id=restore_id,
        sanitized_state_sha256=sanitized_state_sha256,
        restored_at=restored_at,
    )


def restore_backup(
    bundle_path: Path,
    destination_path: Path,
    *,
    restored_at: datetime | None = None,
) -> RestoreReceipt:
    """Verify v3, sanitize a private sibling, and atomically publish an inert restore."""

    bundle, destination, destination_parent_identity = _validate_restore_paths(
        Path(bundle_path),
        Path(destination_path),
    )
    verification = verify_backup(bundle)
    if verification.schema_version != 3 or not verification.production_authority:
        raise BackupVerificationError("restore accepts only verified schema-v3 backups")
    timestamp = datetime.now(UTC) if restored_at is None else restored_at
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise BackupVerificationError("restore timestamp must be timezone-aware")
    timestamp = timestamp.astimezone(UTC)
    restore_id, destination_sha256 = _restore_identity(verification, destination)
    if destination.exists() and destination.is_dir() and any(destination.iterdir()):
        _stage, sanitized_state_sha256, receipt = _probe_existing_restore(
            destination,
            verification=verification,
            restore_id=restore_id,
            destination_sha256=destination_sha256,
        )
        if receipt is not None:
            return receipt
        return _finalize_restore(
            destination,
            verification=verification,
            restore_id=restore_id,
            sanitized_state_sha256=sanitized_state_sha256,
            restored_at=timestamp,
        )
    staging = destination.parent / f".{destination.name}.{restore_id}.staging"
    if staging.exists() or staging.is_symlink():
        return _resume_staged_restore(
            staging,
            destination,
            bundle=bundle,
            verification=verification,
            restore_id=restore_id,
            destination_sha256=destination_sha256,
            restored_at=timestamp,
            destination_parent_identity=destination_parent_identity,
        )
    try:
        staging.mkdir(mode=0o700)
    except OSError as exc:
        raise BackupError("cannot create private restore staging") from exc
    return _resume_staged_restore(
        staging,
        destination,
        bundle=bundle,
        verification=verification,
        restore_id=restore_id,
        destination_sha256=destination_sha256,
        restored_at=timestamp,
        destination_parent_identity=destination_parent_identity,
    )


__all__ = (
    "BackupBundleInputs",
    "BackupError",
    "BackupReason",
    "BackupReceipt",
    "BackupVerification",
    "BackupVerificationError",
    "RestoreReceipt",
    "backup_state",
    "restore_backup",
    "verify_backup",
)
