from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

import firmquant.persistence.backup as backup_module
from firmquant.application.production_identity import DeploymentIdentity, OperationalEvidenceIdentity
from firmquant.persistence.backup import BackupVerificationError
from firmquant.persistence.database import Database
from firmquant.persistence.operational_authority import OperationalAuthorityStore
from firmquant.persistence.repositories import PersistenceConflict
from tests.integration.test_backup_v3_restore import _v3_case


def _authority_members(database: Database) -> tuple[dict[str, object], dict[str, object]]:
    authority = OperationalAuthorityStore(database)
    account = authority.active_account_epoch()
    mode = authority.active_mode_epoch()
    return (
        {
            "schema": "firmquant.account-authority-epoch-backup.v1",
            "epoch": account.epoch,
            "account_id_hash": account.account_id_hash,
            "account_state_sha256": account.account_state_sha256,
            "deployment_identity_sha256": account.deployment_identity_sha256,
            "payload": json.loads(account.payload_json),
            "payload_sha256": account.payload_sha256,
            "created_at": account.created_at.isoformat(),
        },
        {
            "schema": "firmquant.mode-epoch-backup.v1",
            "epoch": mode.epoch,
            "mode": mode.mode.value,
            "deployment_identity_sha256": mode.deployment_identity_sha256,
            "caps_sha256": mode.caps_sha256,
            "payload": json.loads(mode.payload_json),
            "payload_sha256": mode.payload_sha256,
            "created_at": mode.created_at.isoformat(),
        },
    )


def _binding_case(
    tmp_path: Path,
) -> tuple[
    Path,
    DeploymentIdentity,
    OperationalEvidenceIdentity,
    dict[str, object],
    dict[str, object],
]:
    database, _account_path, inputs, _root = _v3_case(tmp_path)
    assert inputs.deployment_identity is not None
    assert inputs.operational_evidence_identity is not None
    account_member, mode_member = _authority_members(database)
    database_path = database.path
    database.close()
    return (
        database_path,
        inputs.deployment_identity,
        inputs.operational_evidence_identity,
        account_member,
        mode_member,
    )


def _verify(
    database_path: Path,
    deployment: DeploymentIdentity,
    evidence: OperationalEvidenceIdentity,
    account_member: Mapping[str, object],
    mode_member: Mapping[str, object],
) -> None:
    backup_module._verify_v3_database_bindings(
        database_path,
        deployment=deployment,
        evidence=evidence,
        account_epoch_payload=account_member,
        mode_epoch_payload=mode_member,
    )


def _damage_database(database_path: Path, sql: str, parameters: tuple[object, ...] = ()) -> None:
    connection = sqlite3.connect(database_path, isolation_level=None)
    try:
        trigger_names = connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
        for (name,) in trigger_names:
            quoted_name = str(name).replace('"', '""')
            connection.execute(f'DROP TRIGGER "{quoted_name}"')
        connection.execute(sql, parameters)
    finally:
        connection.close()


def test_v3_database_binding_wraps_typed_authority_conflict(tmp_path: Path) -> None:
    database_path, deployment, evidence, account_member, mode_member = _binding_case(tmp_path)
    _damage_database(database_path, "DELETE FROM account_authority_active WHERE singleton_id=1")

    with pytest.raises(BackupVerificationError, match="typed authority epoch") as caught:
        _verify(database_path, deployment, evidence, account_member, mode_member)

    assert isinstance(caught.value.__cause__, PersistenceConflict)


def test_v3_database_binding_rejects_epoch_member_different_from_database(
    tmp_path: Path,
) -> None:
    database_path, deployment, evidence, account_member, mode_member = _binding_case(tmp_path)
    account_member["payload_sha256"] = "0" * 64

    with pytest.raises(BackupVerificationError, match="epoch member differs from database"):
        _verify(database_path, deployment, evidence, account_member, mode_member)


def test_v3_database_binding_rejects_coherent_authority_different_from_deployment(
    tmp_path: Path,
) -> None:
    database_path, deployment, evidence, account_member, mode_member = _binding_case(tmp_path)
    mismatched_deployment = replace(deployment, account_id_hash="1" * 64)

    with pytest.raises(BackupVerificationError, match="authority epoch differs from deployment"):
        _verify(database_path, mismatched_deployment, evidence, account_member, mode_member)


def test_v3_database_binding_rejects_deployment_row_drift(tmp_path: Path) -> None:
    database_path, deployment, evidence, account_member, mode_member = _binding_case(tmp_path)
    _damage_database(
        database_path,
        "UPDATE deployment_identities SET account_id_hash=? WHERE deployment_identity_sha256=?",
        ("1" * 64, deployment.sha256),
    )

    with pytest.raises(BackupVerificationError, match="deployment identity differs from database"):
        _verify(database_path, deployment, evidence, account_member, mode_member)


def test_v3_database_binding_rejects_operational_evidence_row_drift(tmp_path: Path) -> None:
    database_path, deployment, evidence, account_member, mode_member = _binding_case(tmp_path)
    _damage_database(
        database_path,
        """
        UPDATE operational_evidence_receipts SET account_state_sha256=?
        WHERE operational_evidence_identity_sha256=?
        """,
        ("1" * 64, evidence.sha256),
    )

    with pytest.raises(BackupVerificationError, match="operational evidence differs from database"):
        _verify(database_path, deployment, evidence, account_member, mode_member)


def test_v3_database_binding_rejects_broker_snapshot_row_drift(tmp_path: Path) -> None:
    database_path, deployment, evidence, account_member, mode_member = _binding_case(tmp_path)
    _damage_database(
        database_path,
        "UPDATE broker_snapshots SET duration_ms=duration_ms+1 WHERE snapshot_id=?",
        (evidence.broker_snapshot_id,),
    )

    with pytest.raises(BackupVerificationError, match="broker snapshot identity differs from database"):
        _verify(database_path, deployment, evidence, account_member, mode_member)
