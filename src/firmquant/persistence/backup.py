"""Atomic SQLite/account-state backup bundles and isolated restore verification."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Never

from firmquant.broker.xtquant_safety import XtQuantSafetyManifest
from firmquant.config import Settings
from firmquant.market_data.calendar_manifest import load_trading_calendar_manifest

from .audit import AuditLedger
from .database import Database, PersistenceError
from .recovery import UquantAccountStateStore
from .repositories import canonical_json
from .schema import CURRENT_SCHEMA_VERSION


class BackupError(PersistenceError):
    """Raised when a consistent atomic backup cannot be created."""


class BackupVerificationError(BackupError):
    """Raised without deleting the backup evidence that failed verification."""


@dataclass(frozen=True, slots=True)
class BackupBundleInputs:
    """Validated production identities copied into a complete recovery bundle."""

    settings: Settings
    config_sha256: str
    safety_manifest_path: Path
    calendar_manifest_path: Path
    active_data_manifest_path: Path
    strategy_data_manifest_path: Path
    firmquant_commit: str
    uquant_commit: str
    account_sha256: str
    decision_id: str
    strategy_session: date

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
        if not isinstance(self.decision_id, str) or not self.decision_id:
            raise ValueError("complete backup decision id is not canonical")
        if type(self.strategy_session) is not date:
            raise TypeError("complete backup strategy session must be date")
        for path in (
            self.safety_manifest_path,
            self.calendar_manifest_path,
            self.active_data_manifest_path,
            self.strategy_data_manifest_path,
        ):
            if not isinstance(path, Path):
                raise TypeError("complete backup member paths must be pathlib.Path")


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
    complete_bundle: bool = False
    decision_id: str | None = None


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


def _database_schema_version(database: Database) -> int:
    value = database.scalar("SELECT max(version) FROM schema_migrations")
    if isinstance(value, bool) or not isinstance(value, int):
        raise BackupVerificationError("backup database schema version is missing or invalid")
    return value


def _verify_database(
    database_path: Path,
    *,
    expected_audit_count: int,
    expected_audit_head: str,
    expected_schema: int,
    required_decision_id: str | None = None,
) -> tuple[int, int, str]:
    with tempfile.TemporaryDirectory(prefix="firmquant-restore-verification-") as temporary:
        restored_path = Path(temporary) / "firmquant.sqlite3"
        shutil.copyfile(database_path, restored_path)
        try:
            restored = Database.open(restored_path)
            try:
                restored.integrity_check()
                schema_version = _database_schema_version(restored)
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
                        raise BackupVerificationError(
                            "complete backup database lacks required frozen decision"
                        )
            finally:
                restored.close()
        except PersistenceError as exc:
            raise BackupVerificationError("isolated backup restore verification failed") from exc
    if schema_version != expected_schema or schema_version != CURRENT_SCHEMA_VERSION:
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
        schema_version=schema_version,
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
        "production_config.json",
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

    config_payload = _json_object(bundle / "production_config.json", label="production config")
    try:
        Settings.model_validate(config_payload)
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
        raise BackupVerificationError("complete backup strategy data manifest is empty")
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
        schema_version=schema_version,
        complete_bundle=True,
        decision_id=decision_id,
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
    raise BackupVerificationError("unsupported backup manifest schema version")


def _validated_config_bytes(settings: Settings) -> bytes:
    payload = settings.model_dump(mode="json")
    rendered = canonical_json(payload).encode("utf-8")
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
    _write_fsynced(temporary_bundle / "production_config.json", _validated_config_bytes(inputs.settings))
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
    deployment = {
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
    deployment: dict[str, object] | None = None
    complete_member_hashes: dict[str, str] | None = None
    if complete_inputs is not None:
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

    restored = Database.open(database_path)
    try:
        restored.integrity_check()
        audit = AuditLedger(restored).verify()
        schema_version = _database_schema_version(restored)
        if complete_inputs is not None:
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
    else:
        assert complete_member_hashes is not None and deployment is not None
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
    "BackupBundleInputs",
    "BackupError",
    "BackupReceipt",
    "BackupVerification",
    "BackupVerificationError",
    "backup_state",
    "verify_backup",
)
