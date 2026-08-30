from __future__ import annotations

from pathlib import Path

import pytest

from firmquant.persistence.database import Database
from firmquant.persistence.schema import CURRENT_SCHEMA_VERSION


def test_account_authority_is_owned_by_central_schema_migration(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        assert CURRENT_SCHEMA_VERSION == 5
        assert database.scalar("SELECT max(version) FROM schema_migrations") == CURRENT_SCHEMA_VERSION
        tables = {
            str(row["name"])
            for row in database.query_all("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        }
        assert {
            "account_bindings",
            "reviewed_account_adjustments",
            "account_bootstrap_operations",
            "account_authority_epochs",
            "account_authority_active",
            "mode_epochs",
            "mode_epoch_active",
        } <= tables
        assert "caps_sha256" in {
            str(row["name"]) for row in database.query_all("PRAGMA table_info(mode_epochs)")
        }

        triggers = {
            str(row["name"])
            for row in database.query_all(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
            )
        }
        assert {
            "account_bindings_reject_update",
            "account_bindings_reject_delete",
            "reviewed_account_adjustments_reject_update",
            "reviewed_account_adjustments_reject_delete",
        } <= triggers
    finally:
        database.close()


def test_account_authority_tables_are_immutable_at_schema_boundary(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        with database.transaction():
            database.write(
                """
                INSERT INTO account_bindings(
                    binding_id, singleton_id, account_id_hash, account_type,
                    broker_snapshot_sha256, account_state_sha256, uquant_commit,
                    uquant_code_fingerprint, data_hash, data_as_of, data_symbols_json,
                    created_at, payload_json, payload_sha256
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "acctbind_" + "1" * 64,
                    "a" * 64,
                    "CASH",
                    "b" * 64,
                    "c" * 64,
                    "1" * 40,
                    "d" * 64,
                    "e" * 64,
                    "2026-01-05",
                    '["sz300308"]',
                    "2026-01-06T08:05:00+00:00",
                    "{}",
                    "f" * 64,
                ),
            )
        with pytest.raises(Exception, match="append-only"), database.transaction():
            database.write("DELETE FROM account_bindings")

        assert tuple(database.query_one("SELECT singleton_id,epoch FROM account_authority_active") or ()) == (
            1,
            1,
        )
        assert database.scalar("SELECT count(*) FROM account_authority_epochs") == 1
    finally:
        database.close()


def test_first_runtime_persistence_creates_mode_epoch_and_pointer_atomically(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        with database.transaction():
            database.write(
                """
                INSERT INTO runtime_state(
                    singleton_id,mode,state,revision,reason,blockers_json,updated_at
                ) VALUES(1,'PAPER','DISARMED',0,'bootstrap','[]','2026-01-06T08:06:00+00:00')
                """
            )
        assert tuple(database.query_one("SELECT epoch,mode FROM mode_epochs") or ()) == (1, "PAPER")
        assert tuple(database.query_one("SELECT singleton_id,epoch FROM mode_epoch_active") or ()) == (1, 1)
    finally:
        database.close()


def test_active_pointers_reject_delete_rollback_and_jump(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        with database.transaction():
            database.write(
                """
                INSERT INTO runtime_state(
                    singleton_id,mode,state,revision,reason,blockers_json,updated_at
                ) VALUES(1,'PAPER','DISARMED',0,'bootstrap','[]','2026-01-06T08:06:00+00:00')
                """
            )
            database.write(
                """
                INSERT INTO mode_epochs(
                    epoch,mode,deployment_identity_sha256,payload_json,payload_sha256,created_at
                ) VALUES(2,'SHADOW',NULL,'{}',?,'2026-01-06T08:07:00+00:00')
                """,
                ("a" * 64,),
            )
            database.write(
                """
                INSERT INTO mode_epochs(
                    epoch,mode,deployment_identity_sha256,payload_json,payload_sha256,created_at
                ) VALUES(3,'CANARY',NULL,'{}',?,'2026-01-06T08:08:00+00:00')
                """,
                ("b" * 64,),
            )
        with pytest.raises(Exception, match="jump"), database.transaction():
            database.write("UPDATE mode_epoch_active SET epoch=3 WHERE singleton_id=1")
        with database.transaction():
            database.write("UPDATE mode_epoch_active SET epoch=2 WHERE singleton_id=1")
        with pytest.raises(Exception, match="rollback"), database.transaction():
            database.write("UPDATE mode_epoch_active SET epoch=1 WHERE singleton_id=1")
        with pytest.raises(Exception, match="delete"), database.transaction():
            database.write("DELETE FROM mode_epoch_active WHERE singleton_id=1")
    finally:
        database.close()


def test_staged_operations_allow_only_forward_stage_changes(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        with database.transaction():
            database.write(
                """
                INSERT INTO account_authority_epochs(
                    epoch,account_id_hash,account_state_sha256,deployment_identity_sha256,
                    source_binding_id,payload_json,payload_sha256,created_at
                ) VALUES(1,?,?,NULL,NULL,'{}',?,'2026-01-06T08:00:00+00:00')
                """,
                ("0" * 64, "1" * 64, "2" * 64),
            )
            database.write(
                """
                INSERT INTO mode_epochs(
                    epoch,mode,deployment_identity_sha256,payload_json,payload_sha256,created_at
                ) VALUES(1,'PAPER',NULL,'{}',?,'2026-01-06T08:00:00+00:00')
                """,
                ("3" * 64,),
            )
            database.write(
                """
                INSERT INTO backup_publication_operations(
                    operation_id,backup_id,stage,reason,manifest_sha256,database_sha256,
                    account_state_sha256,deployment_identity_sha256,
                    operational_evidence_identity_sha256,account_authority_epoch,mode_epoch,
                    bundle_name,payload_json,payload_sha256,created_at,updated_at
                ) VALUES(
                    'publish-1','backup-1','PREPARED','SESSION_CLOSE',?,?,?,?,?,1,1,
                    NULL,'{}',?,'2026-01-06T08:00:00+00:00','2026-01-06T08:00:00+00:00'
                )
                """,
                ("a" * 64, "b" * 64, "c" * 64, "d" * 64, "e" * 64, "f" * 64),
            )
            database.write(
                """
                UPDATE backup_publication_operations
                SET stage='PUBLISHED',updated_at='2026-01-06T08:01:00+00:00'
                WHERE operation_id='publish-1'
                """
            )
        with pytest.raises(Exception, match="forward-only"), database.transaction():
            database.write(
                "UPDATE backup_publication_operations SET stage='PREPARED' WHERE operation_id='publish-1'"
            )
        with pytest.raises(Exception, match="immutable"), database.transaction():
            database.write(
                "UPDATE backup_publication_operations SET backup_id='backup-2' WHERE operation_id='publish-1'"
            )
        with pytest.raises(Exception, match="delete"), database.transaction():
            database.write("DELETE FROM backup_publication_operations WHERE operation_id='publish-1'")
    finally:
        database.close()
