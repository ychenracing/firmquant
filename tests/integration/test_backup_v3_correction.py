from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

import firmquant.persistence.backup as backup_module
from firmquant.persistence.backup import (
    BackupBundleInputs,
    BackupError,
    BackupReason,
    BackupVerificationError,
    backup_state,
    restore_backup,
    verify_backup,
)
from firmquant.persistence.database import Database
from firmquant.persistence.repositories import canonical_json
from firmquant.persistence.schema import MIGRATIONS, apply_migrations
from tests.integration.test_backup_v3_restore import NOW, _v3_case


def _tree_bytes(root: Path) -> dict[str, bytes | None]:
    if not root.exists() and not root.is_symlink():
        return {}
    result: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        result[relative] = path.read_bytes() if path.is_file() and not path.is_symlink() else None
    return result


def _source_backup(tmp_path: Path):
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
    return source, inputs


def _strategy_session_mismatch(database: Database, inputs: BackupBundleInputs) -> BackupBundleInputs:
    strategy_path = inputs.strategy_data_manifest_path
    strategy = json.loads(strategy_path.read_text(encoding="utf-8"))
    strategy["target_session"] = "2026-08-23"
    strategy["observations"][0]["latest_observed_session"] = "2026-08-23"
    strategy_path.write_text(canonical_json(strategy), encoding="utf-8")
    evidence = replace(
        inputs.operational_evidence_identity,
        strategy_data_manifest_sha256=backup_module._sha256_file(strategy_path),
    )
    _register_evidence(database, evidence)
    return replace(inputs, operational_evidence_identity=evidence)


def _legacy_database(path: Path) -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    database = Database(path, connection)
    try:
        apply_migrations(database, migrations=MIGRATIONS[:1])
    finally:
        database.close()


def _register_evidence(database: Database, evidence) -> None:
    deployment = evidence.deployment_identity
    with database.transaction():
        existing = database.query_one(
            "SELECT deployment_identity_sha256 FROM deployment_identities WHERE deployment_identity_sha256=?",
            (deployment.sha256,),
        )
        if existing is None:
            database.write(
                """
                INSERT INTO deployment_identities(
                    deployment_identity_sha256,account_id_hash,account_authority_epoch,
                    mode_epoch,mode,payload_json,payload_sha256,created_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    deployment.sha256,
                    deployment.account_id_hash,
                    deployment.account_authority_epoch,
                    deployment.mode_epoch,
                    deployment.mode.value,
                    deployment.canonical_json,
                    deployment.sha256,
                    NOW.isoformat(),
                ),
            )
        database.write(
            """
            INSERT INTO operational_evidence_receipts(
                receipt_id,operational_evidence_identity_sha256,deployment_identity_sha256,
                account_authority_epoch,mode_epoch,account_state_sha256,broker_snapshot_id,
                strategy_session,phase,kind,payload_json,payload_sha256,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "correction-" + evidence.sha256,
                evidence.sha256,
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
                NOW.isoformat(),
            ),
        )


def test_restore_rejects_lexical_alias_into_source_before_mutation(tmp_path: Path) -> None:
    source, _inputs = _source_backup(tmp_path)
    aliased_source = source.bundle_path.parent / "alias" / ".." / source.bundle_path.name
    (source.bundle_path.parent / "alias").mkdir()
    destination = source.bundle_path / "restored"
    before = _tree_bytes(source.bundle_path)

    with pytest.raises(BackupVerificationError, match=r"source|destination|contain"):
        restore_backup(aliased_source, destination, restored_at=NOW)

    assert _tree_bytes(source.bundle_path) == before


def test_nonempty_destination_probe_preserves_legacy_database_and_all_entries(tmp_path: Path) -> None:
    source, _inputs = _source_backup(tmp_path)
    destination = tmp_path / "incident"
    destination.mkdir()
    _legacy_database(destination / "firmquant.sqlite3")
    (destination / "incident.txt").write_bytes(b"preserve-me")
    before = _tree_bytes(destination)

    with pytest.raises(BackupVerificationError, match="destination"):
        restore_backup(source.bundle_path, destination, restored_at=NOW)

    assert _tree_bytes(destination) == before
    assert not (destination / "firmquant.sqlite3-wal").exists()
    assert not (destination / "firmquant.sqlite3-shm").exists()


def test_arbitrary_staging_probe_is_immutable_and_creates_no_sidecars(tmp_path: Path) -> None:
    source, _inputs = _source_backup(tmp_path)
    destination = tmp_path / "restore-target"
    verification = backup_module.verify_backup(source.bundle_path)
    restore_id, _destination_sha256 = backup_module._restore_identity(verification, destination)
    staging = destination.parent / f".{destination.name}.{restore_id}.staging"
    staging.mkdir()
    _legacy_database(staging / "firmquant.sqlite3")
    (staging / "unknown.bin").write_bytes(b"preserve-staging")
    before = _tree_bytes(staging)

    with pytest.raises(BackupVerificationError, match="staging collision"):
        restore_backup(source.bundle_path, destination, restored_at=NOW)

    assert _tree_bytes(staging) == before
    assert not (staging / "firmquant.sqlite3-wal").exists()
    assert not (staging / "firmquant.sqlite3-shm").exists()


def test_restore_rejects_a_hard_linked_staging_database_before_source_mutation(
    tmp_path: Path,
) -> None:
    source, _inputs = _source_backup(tmp_path)
    destination = tmp_path / "restore-target"
    verification = verify_backup(source.bundle_path)
    restore_id, _destination_sha256 = backup_module._restore_identity(verification, destination)
    staging = destination.parent / f".{destination.name}.{restore_id}.staging"
    staging.mkdir()
    try:
        (staging / "firmquant.sqlite3").hardlink_to(source.bundle_path / "firmquant.sqlite3")
    except OSError:
        pytest.skip("hard links are unavailable on this runner")
    source_before = _tree_bytes(source.bundle_path)

    with pytest.raises(BackupVerificationError, match=r"hard|link|private|staging"):
        restore_backup(source.bundle_path, destination, restored_at=NOW)

    assert _tree_bytes(source.bundle_path) == source_before
    assert not destination.exists()


@pytest.mark.parametrize("entry_kind", ["file", "directory", "file-symlink"])
def test_staged_restore_retry_rejects_unexpected_entries_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_kind: str,
) -> None:
    source, _inputs = _source_backup(tmp_path)
    destination = tmp_path / "restore-target"
    real_fsync_directory = backup_module._fsync_directory
    failed = False

    def fail_staging_fsync(path: Path) -> None:
        nonlocal failed
        if path.name.endswith(".staging") and not failed:
            failed = True
            raise OSError("injected staging fsync failure")
        real_fsync_directory(path)

    monkeypatch.setattr(backup_module, "_fsync_directory", fail_staging_fsync)
    with pytest.raises(BackupError, match="publish"):
        restore_backup(source.bundle_path, destination, restored_at=NOW)
    staging = next(tmp_path.glob(".restore-target.*.staging"))
    unexpected = staging / "unexpected-incident-evidence"
    external = tmp_path / "external-incident-evidence"
    if entry_kind == "file":
        unexpected.write_bytes(b"preserve unexpected evidence")
    elif entry_kind == "directory":
        unexpected.mkdir()
        (unexpected / "nested.bin").write_bytes(b"preserve nested evidence")
    else:
        external.write_bytes(b"preserve external evidence")
        try:
            unexpected.symlink_to(external)
        except OSError:
            pytest.skip("file symlinks are unavailable on this runner")
    staging_before = _tree_bytes(staging)
    external_before = external.read_bytes() if external.exists() else None

    monkeypatch.setattr(backup_module, "_fsync_directory", real_fsync_directory)
    with pytest.raises(BackupVerificationError, match=r"staging|entries|member|private"):
        restore_backup(source.bundle_path, destination, restored_at=NOW)

    assert _tree_bytes(staging) == staging_before
    assert not destination.exists()
    if external_before is not None:
        assert external.read_bytes() == external_before


def test_restore_reverifies_copied_source_before_staged_database_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _inputs = _source_backup(tmp_path)
    destination = tmp_path / "restore-target"
    real_copy = backup_module._copy_fsynced
    swapped = False

    def swap_after_initial_verification(source_path: Path, target: Path, *, label: str) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            (source.bundle_path / "deployment_identity.json").write_text("{}", encoding="utf-8")
        real_copy(source_path, target, label=label)

    monkeypatch.setattr(backup_module, "_copy_fsynced", swap_after_initial_verification)
    with pytest.raises(BackupVerificationError, match=r"SHA-256|identity|manifest"):
        restore_backup(source.bundle_path, destination, restored_at=NOW)

    assert not destination.exists()
    staging = next(tmp_path.glob(".restore-target.*.staging"))
    staged_database_path = staging / "firmquant.sqlite3"
    if staged_database_path.exists():
        staged_database = Database.open_read_only(staged_database_path, immutable=True)
        try:
            assert staged_database.scalar("SELECT state FROM runtime_state WHERE singleton_id=1") == "READY"
        finally:
            staged_database.close()


def test_backup_creation_rejects_strategy_manifest_session_different_from_evidence(
    tmp_path: Path,
) -> None:
    database, account, inputs, root = _v3_case(tmp_path)
    mismatched = _strategy_session_mismatch(database, inputs)
    try:
        with pytest.raises(BackupError, match=r"data manifest|strategy.*session"):
            backup_state(
                database,
                root,
                account_state_path=account,
                complete_inputs=mismatched,
                created_at=NOW,
            )
    finally:
        database.close()


def test_backup_verification_rejects_strategy_manifest_session_different_from_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, account, inputs, root = _v3_case(tmp_path)
    mismatched = _strategy_session_mismatch(database, inputs)
    real_validate = backup_module._validate_data_manifests
    monkeypatch.setattr(
        backup_module,
        "_validate_data_manifests",
        lambda _active, _strategy: mismatched.strategy_session,
    )
    try:
        source = backup_state(
            database,
            root,
            account_state_path=account,
            complete_inputs=mismatched,
            created_at=NOW,
        )
    finally:
        database.close()
    monkeypatch.setattr(backup_module, "_validate_data_manifests", real_validate)

    with pytest.raises(BackupVerificationError, match=r"data manifest|strategy.*session"):
        verify_backup(source.bundle_path)


def test_backup_creation_rejects_broker_snapshot_session_different_from_evidence(
    tmp_path: Path,
) -> None:
    database, account, inputs, root = _v3_case(tmp_path, snapshot_session="2026-08-23")
    evidence = inputs.operational_evidence_identity
    assert evidence is not None
    try:
        with pytest.raises(BackupError, match=r"broker snapshot|identity"):
            backup_state(
                database,
                root,
                account_state_path=account,
                complete_inputs=inputs,
                created_at=NOW,
            )
    finally:
        database.close()


def test_backup_verification_rejects_broker_snapshot_session_different_from_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, account, inputs, root = _v3_case(tmp_path, snapshot_session="2026-08-23")
    evidence = inputs.operational_evidence_identity
    assert evidence is not None
    real_verify_bindings = backup_module._verify_v3_database_bindings
    monkeypatch.setattr(backup_module, "_verify_v3_database_bindings", lambda *args, **kwargs: None)
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
    monkeypatch.setattr(backup_module, "_verify_v3_database_bindings", real_verify_bindings)

    with pytest.raises(BackupVerificationError, match=r"broker snapshot|identity"):
        verify_backup(source.bundle_path)


def test_restore_retry_completes_exact_partial_member_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _inputs = _source_backup(tmp_path)
    destination = tmp_path / "restore-target"
    real_copy = backup_module._copy_fsynced
    copied = 0

    def fail_after_three(source_path: Path, target: Path, *, label: str) -> None:
        nonlocal copied
        if copied == 3:
            target.write_bytes(source_path.read_bytes()[:17])
            raise BackupError("injected restore member copy failure")
        real_copy(source_path, target, label=label)
        copied += 1

    monkeypatch.setattr(backup_module, "_copy_fsynced", fail_after_three)
    with pytest.raises(BackupError, match="injected restore"):
        restore_backup(source.bundle_path, destination, restored_at=NOW)
    staging = next(tmp_path.glob(".restore-target.*.staging"))
    before = _tree_bytes(staging)
    assert 0 < len(before) < 12

    monkeypatch.setattr(backup_module, "_copy_fsynced", real_copy)
    receipt = restore_backup(source.bundle_path, destination, restored_at=NOW)
    assert receipt.destination == destination
    assert not staging.exists()


def test_backup_retry_completes_exact_partial_member_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, account, inputs, root = _v3_case(tmp_path)
    real_copy = backup_module._copy_fsynced
    copied = 0

    def fail_after_two(source_path: Path, target: Path, *, label: str) -> None:
        nonlocal copied
        if copied == 2:
            target.write_bytes(source_path.read_bytes()[:17])
            raise BackupError("injected backup member copy failure")
        real_copy(source_path, target, label=label)
        copied += 1

    monkeypatch.setattr(backup_module, "_copy_fsynced", fail_after_two)
    try:
        with pytest.raises(BackupError, match="injected backup"):
            backup_state(
                database,
                root,
                account_state_path=account,
                complete_inputs=inputs,
                created_at=NOW,
            )
        staging = next(root.glob(".*.staging"))
        before = _tree_bytes(staging)
        assert 0 < len(before) < 12

        monkeypatch.setattr(backup_module, "_copy_fsynced", real_copy)
        receipt = backup_state(
            database,
            root,
            account_state_path=account,
            complete_inputs=inputs,
            created_at=NOW,
        )
    finally:
        database.close()
    assert receipt.bundle_path.is_dir()
    assert not staging.exists()


@pytest.mark.parametrize(
    ("reason", "phase"),
    [
        (BackupReason.MODE_TRANSITION, "MODE_TRANSITION"),
        (BackupReason.ACCOUNT_REBASELINE, "ACCOUNT_REBASELINE"),
    ],
)
def test_non_session_v3_backup_allows_null_decision(
    tmp_path: Path,
    reason: BackupReason,
    phase: str,
) -> None:
    database, account, inputs, root = _v3_case(tmp_path)
    evidence = replace(
        inputs.operational_evidence_identity,
        decision_id=None,
        phase=phase,
        kind="BACKUP",
    )
    _register_evidence(database, evidence)
    corrected = replace(
        inputs,
        reason=reason,
        decision_id=None,
        operational_evidence_identity=evidence,
    )
    try:
        receipt = backup_state(
            database,
            root,
            account_state_path=account,
            complete_inputs=corrected,
            created_at=NOW,
        )
    finally:
        database.close()
    assert backup_module.verify_backup(receipt.bundle_path).decision_id is None


@pytest.mark.parametrize(
    ("reason", "phase", "kind", "decision_id"),
    [
        (BackupReason.SESSION_CLOSE, "STARTUP", "SMOKE", "decision-required"),
        (BackupReason.SESSION_CLOSE, "EXECUTION", "SHADOW_EXECUTION", "decision-required"),
        (BackupReason.SESSION_CLOSE, "EOD", "BACKUP", None),
        (BackupReason.MODE_TRANSITION, "EOD", "BACKUP", None),
        (BackupReason.ACCOUNT_REBASELINE, "EOD", "BACKUP", None),
    ],
)
def test_backup_inputs_reject_reason_fact_relabeling(
    tmp_path: Path,
    reason: BackupReason,
    phase: str,
    kind: str,
    decision_id: str | None,
) -> None:
    database, _account, inputs, _root = _v3_case(tmp_path)
    database.close()
    evidence_decision = inputs.decision_id if decision_id is not None else None
    evidence = replace(
        inputs.operational_evidence_identity,
        decision_id=evidence_decision,
        phase=phase,
        kind=kind,
    )
    with pytest.raises(ValueError, match=r"reason|phase|kind|decision"):
        BackupBundleInputs(
            settings=inputs.settings,
            config_path=inputs.config_path,
            config_sha256=inputs.config_sha256,
            safety_manifest_path=inputs.safety_manifest_path,
            calendar_manifest_path=inputs.calendar_manifest_path,
            active_data_manifest_path=inputs.active_data_manifest_path,
            strategy_data_manifest_path=inputs.strategy_data_manifest_path,
            firmquant_commit=inputs.firmquant_commit,
            uquant_commit=inputs.uquant_commit,
            account_sha256=inputs.account_sha256,
            decision_id=None if decision_id is None else inputs.decision_id,
            strategy_session=inputs.strategy_session,
            reason=reason,
            deployment_identity=inputs.deployment_identity,
            operational_evidence_identity=evidence,
        )


def test_verify_rejects_arbitrary_manifest_backup_id_and_directory_name(tmp_path: Path) -> None:
    source, _inputs = _source_backup(tmp_path)
    manifest_path = source.bundle_path / "manifest.json"
    manifest = backup_module._manifest(manifest_path)
    manifest["backup_id"] = "backup-" + "0" * 64
    manifest_path.write_text(canonical_json(manifest), encoding="utf-8")
    with pytest.raises(BackupVerificationError, match="backup id"):
        backup_module.verify_backup(source.bundle_path)

    second = tmp_path / "second"
    second.mkdir()
    source2, _inputs2 = _source_backup(second)
    renamed = source2.bundle_path.parent / ("backup-" + "7" * 64)
    source2.bundle_path.rename(renamed)
    with pytest.raises(BackupVerificationError, match=r"directory|basename|backup id"):
        backup_module.verify_backup(renamed)


def test_arbitrary_non_json_data_manifests_cannot_be_authoritative(tmp_path: Path) -> None:
    database, account, inputs, root = _v3_case(tmp_path)
    inputs.active_data_manifest_path.write_bytes(b"not-json")
    inputs.strategy_data_manifest_path.write_bytes(b"also-not-json")
    evidence = replace(
        inputs.operational_evidence_identity,
        active_data_generation_sha256=backup_module._sha256_file(inputs.active_data_manifest_path),
        strategy_data_manifest_sha256=backup_module._sha256_file(inputs.strategy_data_manifest_path),
    )
    _register_evidence(database, evidence)
    corrupted = replace(inputs, operational_evidence_identity=evidence)
    try:
        with pytest.raises(BackupError, match=r"data.*manifest|JSON"):
            backup_state(
                database,
                root,
                account_state_path=account,
                complete_inputs=corrupted,
                created_at=NOW,
            )
    finally:
        database.close()


def test_backup_receipt_failure_leaves_durable_published_operation_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, account, inputs, root = _v3_case(tmp_path)
    real_write = Database.write
    injected = False

    def fail_first_receipt(
        target: Database,
        sql: str,
        parameters: tuple[object, ...] = (),
    ):
        nonlocal injected
        if target is database and "INSERT INTO backup_receipts" in sql and not injected:
            injected = True
            raise sqlite3.OperationalError("injected backup receipt failure")
        return real_write(target, sql, parameters)

    monkeypatch.setattr(Database, "write", fail_first_receipt)
    try:
        with pytest.raises(sqlite3.OperationalError, match="injected backup receipt"):
            backup_state(
                database,
                root,
                account_state_path=account,
                complete_inputs=inputs,
                created_at=NOW,
            )
        operation = database.query_one("SELECT stage,bundle_name FROM backup_publication_operations")
        assert operation is not None
        assert tuple(operation) == ("PUBLISHED", next(root.glob("backup-*")).name)

        receipt = backup_state(
            database,
            root,
            account_state_path=account,
            complete_inputs=inputs,
            created_at=NOW,
        )
        assert (
            database.scalar("SELECT count(*) FROM backup_receipts WHERE backup_id=?", (receipt.backup_id,))
            == 1
        )
    finally:
        database.close()


def test_restore_receipt_failure_leaves_durable_published_operation_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _inputs = _source_backup(tmp_path)
    destination = tmp_path / "restore-target"
    real_write = Database.write
    injected = False

    def fail_first_receipt(
        target: Database,
        sql: str,
        parameters: tuple[object, ...] = (),
    ):
        nonlocal injected
        if (
            target.path == destination / "firmquant.sqlite3"
            and "INSERT INTO restore_receipts" in sql
            and not injected
        ):
            injected = True
            raise sqlite3.OperationalError("injected restore receipt failure")
        return real_write(target, sql, parameters)

    monkeypatch.setattr(Database, "write", fail_first_receipt)
    with pytest.raises(sqlite3.OperationalError, match="injected restore receipt"):
        restore_backup(source.bundle_path, destination, restored_at=NOW)

    preserved = Database.open_read_only(destination / "firmquant.sqlite3", immutable=True)
    try:
        assert preserved.scalar("SELECT stage FROM restore_operations") == "PUBLISHED"
    finally:
        preserved.close()

    receipt = restore_backup(source.bundle_path, destination, restored_at=NOW)
    restored = Database.open_read_only(destination / "firmquant.sqlite3", immutable=True)
    try:
        assert (
            restored.scalar("SELECT count(*) FROM restore_receipts WHERE restore_id=?", (receipt.restore_id,))
            == 1
        )
    finally:
        restored.close()


@pytest.mark.parametrize(
    ("payload_json", "payload_sha256"),
    [
        (None, "0" * 64),
        ('{ "schema": "firmquant.account-authority-epoch.v1" }', None),
        (
            '{"schema":"firmquant.account-authority-epoch.v1",'
            '"schema":"firmquant.account-authority-epoch.v1"}',
            None,
        ),
        ('{"schema":"firmquant.account-authority-epoch.v1","weight":1.5}', None),
        (
            canonical_json(
                {
                    "schema": "firmquant.account-authority-epoch.v1",
                    "epoch": 1,
                    "account_id_hash": "1" * 64,
                    "account_state_sha256": "2" * 64,
                    "deployment_identity_sha256": "3" * 64,
                    "created_at": NOW,
                }
            ),
            None,
        ),
    ],
    ids=("bad-sha", "noncanonical", "duplicate", "float", "semantic-mismatch"),
)
def test_typed_account_epoch_corruption_cannot_yield_production_authority(
    tmp_path: Path,
    payload_json: str | None,
    payload_sha256: str | None,
) -> None:
    database, account, inputs, root = _v3_case(tmp_path)
    if payload_json is None:
        current = database.query_one("SELECT payload_json FROM account_authority_epochs WHERE epoch=1")
        assert current is not None
        rendered = str(current["payload_json"])
    else:
        rendered = payload_json
    digest = payload_sha256 or backup_module.hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    with database.transaction():
        database.write("DROP TRIGGER account_authority_epochs_reject_update")
        database.write(
            "UPDATE account_authority_epochs SET payload_json=?,payload_sha256=? WHERE epoch=1",
            (rendered, digest),
        )
    try:
        with pytest.raises(BackupError, match=r"authority epoch|payload|canonical"):
            backup_state(
                database,
                root,
                account_state_path=account,
                complete_inputs=inputs,
                created_at=NOW,
            )
    finally:
        database.close()


def test_mode_epoch_semantics_must_match_deployment_identity(tmp_path: Path) -> None:
    database, account, inputs, root = _v3_case(tmp_path)
    payload = canonical_json(
        {
            "schema": "firmquant.mode-epoch.v1",
            "epoch": 2,
            "mode": "PAPER",
            "deployment_identity_sha256": inputs.deployment_identity.sha256,
            "caps_sha256": "0" * 64,
            "created_at": NOW,
        }
    )
    with database.transaction():
        database.write("DROP TRIGGER mode_epochs_reject_update")
        database.write(
            "UPDATE mode_epochs SET caps_sha256=?,payload_json=?,payload_sha256=? WHERE epoch=2",
            (
                "0" * 64,
                payload,
                backup_module.hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            ),
        )
    try:
        with pytest.raises(BackupError, match=r"mode epoch|deployment|caps"):
            backup_state(
                database,
                root,
                account_state_path=account,
                complete_inputs=inputs,
                created_at=NOW,
            )
    finally:
        database.close()


@pytest.mark.parametrize(
    ("boundary", "statement"),
    [
        ("PREPARED", "INSERT INTO backup_publication_operations"),
        ("PUBLISHED", "SET stage='PUBLISHED'"),
        ("RECEIPT_COMMITTED", "SET stage='RECEIPT_COMMITTED'"),
    ],
)
def test_backup_exact_retry_recovers_each_sql_publication_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    statement: str,
) -> None:
    database, account, inputs, root = _v3_case(tmp_path)
    real_write = Database.write
    injected = False

    def fail_boundary(
        target: Database,
        sql: str,
        parameters: Sequence[object] = (),
    ):
        nonlocal injected
        if target is database and statement in sql and not injected:
            injected = True
            raise sqlite3.OperationalError(f"injected backup {boundary} failure")
        return real_write(target, sql, parameters)

    monkeypatch.setattr(Database, "write", fail_boundary)
    try:
        with pytest.raises(sqlite3.OperationalError, match=f"injected backup {boundary}"):
            backup_state(
                database,
                root,
                account_state_path=account,
                complete_inputs=inputs,
                created_at=NOW,
            )
        receipt = backup_state(
            database,
            root,
            account_state_path=account,
            complete_inputs=inputs,
            created_at=NOW,
        )
        assert (
            database.scalar("SELECT count(*) FROM backup_receipts WHERE backup_id=?", (receipt.backup_id,))
            == 1
        )
        assert (
            database.scalar(
                "SELECT stage FROM backup_publication_operations WHERE backup_id=?",
                (receipt.backup_id,),
            )
            == "RECEIPT_COMMITTED"
        )
    finally:
        database.close()


@pytest.mark.parametrize(
    ("boundary", "statement"),
    [
        ("PREPARED", "INSERT INTO restore_operations"),
        ("PUBLISHED", "SET stage='PUBLISHED'"),
        ("RECEIPT_COMMITTED", "SET stage='RECEIPT_COMMITTED'"),
    ],
)
def test_restore_exact_retry_recovers_each_sql_publication_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    statement: str,
) -> None:
    source, _inputs = _source_backup(tmp_path)
    destination = tmp_path / "restore-boundary"
    real_write = Database.write
    injected = False

    def fail_boundary(
        target: Database,
        sql: str,
        parameters: Sequence[object] = (),
    ):
        nonlocal injected
        if "restore-boundary" in str(target.path.parent) and statement in sql and not injected:
            injected = True
            raise sqlite3.OperationalError(f"injected restore {boundary} failure")
        return real_write(target, sql, parameters)

    monkeypatch.setattr(Database, "write", fail_boundary)
    with pytest.raises(sqlite3.OperationalError, match=f"injected restore {boundary}"):
        restore_backup(source.bundle_path, destination, restored_at=NOW)

    receipt = restore_backup(source.bundle_path, destination, restored_at=NOW)
    restored = Database.open_read_only(destination / "firmquant.sqlite3", immutable=True)
    try:
        assert (
            restored.scalar("SELECT count(*) FROM restore_receipts WHERE restore_id=?", (receipt.restore_id,))
            == 1
        )
        assert (
            restored.scalar("SELECT stage FROM restore_operations WHERE restore_id=?", (receipt.restore_id,))
            == "RECEIPT_COMMITTED"
        )
    finally:
        restored.close()


def _published_restore_without_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, str]:
    source, _inputs = _source_backup(tmp_path)
    destination = tmp_path / "published-restore"
    real_write = Database.write
    injected = False

    def fail_receipt(
        target: Database,
        sql: str,
        parameters: Sequence[object] = (),
    ):
        nonlocal injected
        if (
            target.path == destination / "firmquant.sqlite3"
            and "INSERT INTO restore_receipts" in sql
            and not injected
        ):
            injected = True
            raise sqlite3.OperationalError("injected published restore receipt failure")
        return real_write(target, sql, parameters)

    monkeypatch.setattr(Database, "write", fail_receipt)
    with pytest.raises(sqlite3.OperationalError, match="published restore receipt"):
        restore_backup(source.bundle_path, destination, restored_at=NOW)
    database = Database.open_read_only(destination / "firmquant.sqlite3", immutable=True)
    try:
        operation = database.query_one("SELECT restore_id,stage FROM restore_operations")
        assert operation is not None
        assert str(operation["stage"]) == "PUBLISHED"
        return source.bundle_path, destination, str(operation["restore_id"])
    finally:
        database.close()


@pytest.mark.parametrize("entry_kind", ["file", "directory"])
def test_published_restore_with_unrelated_entry_is_rejected_immutably(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_kind: str,
) -> None:
    source, destination, _restore_id = _published_restore_without_receipt(tmp_path, monkeypatch)
    unrelated = destination / "unrelated-evidence"
    if entry_kind == "file":
        unrelated.write_bytes(b"preserve incident evidence")
    else:
        unrelated.mkdir()
        (unrelated / "nested.bin").write_bytes(b"preserve nested evidence")
    before = _tree_bytes(destination)

    with pytest.raises(BackupVerificationError, match=r"destination|directory|publication"):
        restore_backup(source, destination, restored_at=NOW)

    assert _tree_bytes(destination) == before


@pytest.mark.parametrize("final_directory_name", [None, "wrong-published-name"])
def test_published_restore_with_wrong_final_name_is_rejected_immutably(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    final_directory_name: str | None,
) -> None:
    source, destination, restore_id = _published_restore_without_receipt(tmp_path, monkeypatch)
    database = Database.open(destination / "firmquant.sqlite3")
    try:
        with database.transaction():
            database.write("DROP TRIGGER restore_operations_forward_only")
            database.write("DROP TRIGGER restore_operations_publication_output_guard")
            database.write(
                "UPDATE restore_operations SET final_directory_name=? WHERE restore_id=?",
                (final_directory_name, restore_id),
            )
    finally:
        database.close()
    before = _tree_bytes(destination)

    with pytest.raises(BackupVerificationError, match=r"destination|directory|publication"):
        restore_backup(source, destination, restored_at=NOW)

    assert _tree_bytes(destination) == before


@pytest.mark.parametrize("member_name", ["production_config.toml", "manifest.json"])
def test_published_restore_with_changed_non_database_member_is_rejected_immutably(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    member_name: str,
) -> None:
    source, destination, _restore_id = _published_restore_without_receipt(tmp_path, monkeypatch)
    member = destination / member_name
    member.write_bytes(member.read_bytes() + b"\nchanged after publication\n")
    before = _tree_bytes(destination)

    with pytest.raises(BackupVerificationError, match=r"destination|member|manifest|identity"):
        restore_backup(source, destination, restored_at=NOW)

    assert _tree_bytes(destination) == before


def test_completed_restore_retry_validates_all_receipt_facts(
    tmp_path: Path,
) -> None:
    source, _inputs = _source_backup(tmp_path)
    destination = tmp_path / "restore-target"
    receipt = restore_backup(source.bundle_path, destination, restored_at=NOW)
    database = Database.open(destination / "firmquant.sqlite3")
    try:
        with database.transaction():
            database.write("DROP TRIGGER restore_receipts_reject_update")
            database.write(
                "UPDATE restore_receipts SET original_audit_head=? WHERE restore_id=?",
                ("f" * 64, receipt.restore_id),
            )
    finally:
        database.close()
    before = _tree_bytes(destination)

    with pytest.raises(BackupVerificationError, match=r"receipt|identity|proof"):
        restore_backup(source.bundle_path, destination, restored_at=NOW)

    assert _tree_bytes(destination) == before


def test_restore_publication_never_opens_a_substituted_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _inputs = _source_backup(tmp_path)
    destination = tmp_path / "restore-target"
    incident = tmp_path / "incident"
    incident.mkdir()
    incident_database = incident / "firmquant.sqlite3"
    _legacy_database(incident_database)
    before = _tree_bytes(incident)
    real_replace = backup_module.os.replace
    substituted = False

    def substitute_at_publication(source_path: Path, destination_path: Path) -> None:
        nonlocal substituted
        if not substituted and source_path.name.endswith(".staging") and destination_path == destination:
            substituted = True
            preserved = source_path.with_name(source_path.name + ".preserved")
            real_replace(source_path, preserved)
            real_replace(incident, destination_path)
            return
        real_replace(source_path, destination_path)

    if backup_module.os.name == "nt":

        def substitute_move_file_ex(source_path: Path, destination_path: Path, _flags: int) -> bool:
            substitute_at_publication(source_path, destination_path)
            return True

        monkeypatch.setattr(backup_module, "_move_file_ex", substitute_move_file_ex)
    else:
        monkeypatch.setattr(backup_module.os, "replace", substitute_at_publication)

    with pytest.raises(BackupError, match=r"publication|directory|identity"):
        restore_backup(source.bundle_path, destination, restored_at=NOW)

    assert substituted is True
    assert not incident.exists()
    assert _tree_bytes(destination) == before
    assert not (destination / "firmquant.sqlite3-wal").exists()
    assert not (destination / "firmquant.sqlite3-shm").exists()
