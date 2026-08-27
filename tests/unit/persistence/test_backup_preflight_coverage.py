from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

import firmquant.persistence.backup as backup
from firmquant.config import Settings

NOW = datetime(2026, 8, 25, 8, tzinfo=UTC)
SESSION = date(2026, 8, 25)


def _inputs(tmp_path: Path) -> backup.BackupBundleInputs:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "production.toml"
    config.write_text("", encoding="utf-8")
    members: list[Path] = []
    for name in ("safety.json", "calendar.json", "active.json", "strategy.json"):
        path = tmp_path / name
        path.write_text("{}", encoding="utf-8")
        members.append(path)
    return backup.BackupBundleInputs(
        settings=Settings(),
        config_path=config,
        config_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
        safety_manifest_path=members[0],
        calendar_manifest_path=members[1],
        active_data_manifest_path=members[2],
        strategy_data_manifest_path=members[3],
        firmquant_commit="a" * 40,
        uquant_commit="b" * 40,
        account_sha256="c" * 64,
        decision_id="decision-test",
        strategy_session=SESSION,
    )


def test_backup_bundle_inputs_reject_noncanonical_identity_and_member_types(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    cases: tuple[tuple[dict[str, object], type[Exception], str], ...] = (
        ({"settings": object()}, TypeError, "validated Settings"),
        ({"config_sha256": "g" * 64}, ValueError, "config SHA-256"),
        ({"account_sha256": "z" * 64}, ValueError, "account SHA-256"),
        ({"firmquant_commit": "a" * 39}, ValueError, "firmquant commit"),
        ({"uquant_commit": "b" * 39}, ValueError, "uquant commit"),
        ({"decision_id": ""}, ValueError, "decision id"),
        ({"strategy_session": datetime(2026, 8, 25, tzinfo=UTC)}, TypeError, "strategy session"),
        ({"safety_manifest_path": "not-a-path"}, TypeError, "member paths"),
    )
    for changes, error_type, pattern in cases:
        with pytest.raises(error_type, match=pattern):
            replace(inputs, **changes)


def test_backup_file_copy_and_write_helpers_fail_closed(tmp_path: Path) -> None:
    destination = tmp_path / "member.bin"
    backup._write_fsynced(destination, b"first")
    assert destination.read_bytes() == b"first"
    with pytest.raises(backup.BackupError, match="cannot write backup member"):
        backup._write_fsynced(destination, b"second")

    with pytest.raises(backup.BackupError, match="regular non-symlink"):
        backup._copy_fsynced(tmp_path / "missing", tmp_path / "copy.bin", label="test member")

    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    copied = tmp_path / "copied.bin"
    backup._copy_fsynced(source, copied, label="test member")
    assert copied.read_bytes() == b"payload"
    backup._fsync_directory(tmp_path)


def test_backup_config_digest_mismatch_is_rejected(tmp_path: Path) -> None:
    inputs = replace(_inputs(tmp_path), config_sha256="0" * 64)
    with pytest.raises(backup.BackupError, match="identity does not match"):
        backup._validated_config_bytes(inputs)


def test_verify_backup_rejects_external_digest_and_unknown_schema(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    manifest = bundle / "manifest.json"
    manifest.write_text('{"schema_version":3}', encoding="utf-8")
    observed = hashlib.sha256(manifest.read_bytes()).hexdigest()

    with pytest.raises(backup.BackupVerificationError, match="external receipt"):
        backup.verify_backup(bundle, expected_manifest_sha256="0" * 64)
    with pytest.raises(backup.BackupVerificationError, match="unsupported backup manifest"):
        backup.verify_backup(bundle, expected_manifest_sha256=observed)


def test_legacy_bundle_manifest_contract_rejects_early_shape_errors(tmp_path: Path) -> None:
    with pytest.raises(backup.BackupVerificationError, match="manifest fields"):
        backup._verify_legacy_bundle(tmp_path, {}, manifest_sha256="0" * 64)

    root = {
        "schema_version": 1,
        "backup_id": "backup-test",
        "created_at": NOW.isoformat(),
        "database": {},
        "account_state": None,
        "operational_schema_version": 1,
        "audit": {"count": 0, "head_hash": "0" * 64},
    }
    with pytest.raises(backup.BackupVerificationError, match="database manifest fields"):
        backup._verify_legacy_bundle(tmp_path, root, manifest_sha256="0" * 64)

    root["database"] = {"filename": "wrong.sqlite3", "sha256": "0" * 64}
    with pytest.raises(backup.BackupVerificationError, match="filename is not canonical"):
        backup._verify_legacy_bundle(tmp_path, root, manifest_sha256="0" * 64)


def test_complete_bundle_manifest_contract_rejects_early_shape_errors(tmp_path: Path) -> None:
    with pytest.raises(backup.BackupVerificationError, match="manifest fields"):
        backup._verify_complete_bundle(tmp_path, {}, manifest_sha256="0" * 64)

    root = {
        "schema_version": 2,
        "backup_id": "backup-test",
        "created_at": NOW.isoformat(),
        "members": {},
        "operational_schema_version": 1,
        "audit": {"count": 0, "head_hash": "0" * 64},
        "deployment": {},
    }
    with pytest.raises(backup.BackupVerificationError, match="member set"):
        backup._verify_complete_bundle(tmp_path, root, manifest_sha256="0" * 64)


def test_backup_state_preflight_rejects_invalid_destination_time_account_and_overwrite(
    tmp_path: Path,
) -> None:
    database = object()
    with pytest.raises(backup.BackupError, match="destination"):
        backup.backup_state(database, tmp_path / "missing")  # type: ignore[arg-type]

    root = tmp_path / "backups"
    root.mkdir()
    with pytest.raises(backup.BackupError, match="timezone-aware"):
        backup.backup_state(
            database,  # type: ignore[arg-type]
            root,
            created_at=datetime(2026, 8, 25, 8),
        )

    inputs = _inputs(tmp_path / "complete")
    with pytest.raises(backup.BackupError, match="requires uquant AccountState"):
        backup.backup_state(
            database,  # type: ignore[arg-type]
            root,
            created_at=NOW,
            complete_inputs=inputs,
        )

    existing = root / ("backup-" + NOW.strftime("%Y%m%dT%H%M%S%fZ"))
    existing.mkdir()
    with pytest.raises(backup.BackupError, match="overwrite"):
        backup.backup_state(database, root, created_at=NOW)  # type: ignore[arg-type]


def test_database_schema_version_requires_integer() -> None:
    class MissingSchema:
        def scalar(self, _query: str) -> object:
            return None

    with pytest.raises(backup.BackupVerificationError, match="schema version"):
        backup._database_schema_version(MissingSchema())  # type: ignore[arg-type]
