from __future__ import annotations

from pathlib import Path

import pytest

from firmquant.persistence.database import Database
from firmquant.persistence.schema import CURRENT_SCHEMA_VERSION


def _seed_operational_epochs(database: Database) -> None:
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
                SET stage='PUBLISHED',bundle_name='backup-1.fqbackup',
                    updated_at='2026-01-06T08:01:00+00:00'
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


def test_snapshot_timing_schema_rejects_null_duration_with_timestamps(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        with pytest.raises(Exception, match="CHECK constraint"), database.transaction():
            database.write(
                """
                INSERT INTO broker_snapshots(
                    snapshot_id,account_id_hash,account_type,session_date,captured_at,
                    broker_event_watermark,raw_payload_sha256,complete,
                    started_at,completed_at,duration_ms
                ) VALUES('snapshot-partial',?,'CASH','2026-01-06',?,0,?,1,?,?,NULL)
                """,
                (
                    "a" * 64,
                    "2026-01-06T08:00:00+00:00",
                    "b" * 64,
                    "2026-01-06T07:59:59+00:00",
                    "2026-01-06T08:00:00+00:00",
                ),
            )
    finally:
        database.close()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("bundle_schema_version", 3),
        ("operational_schema_version", 5),
        ("reason", "SESSION_CLOSE"),
        ("deployment_identity_sha256", "4" * 64),
        ("operational_evidence_identity_sha256", "5" * 64),
        ("account_authority_epoch", 1),
        ("mode_epoch", 1),
        ("broker_snapshot_id", "snapshot-1"),
        ("broker_snapshot_sha256", "6" * 64),
    ],
)
def test_backup_receipt_rejects_each_partial_v3_extension_singleton(
    tmp_path: Path, column: str, value: object
) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        with pytest.raises(Exception, match="v3 tuple"), database.transaction():
            database.write(
                f"""
                INSERT INTO backup_receipts(
                    backup_id,database_sha256,account_state_sha256,manifest_json,
                    manifest_sha256,created_at,verification_status,{column}
                ) VALUES('partial-v3',?,?,'{{}}',?,?,'VERIFIED',?)
                """,
                (
                    "a" * 64,
                    "b" * 64,
                    "c" * 64,
                    "2026-01-06T08:00:00+00:00",
                    value,
                ),
            )
    finally:
        database.close()


@pytest.mark.parametrize(
    ("table", "insert_sql", "parameters"),
    [
        (
            "account_rebaseline_operations",
            """
            INSERT INTO account_rebaseline_operations(
                operation_id,stage,source_epoch,target_epoch,account_id_hash,
                account_before_sha256,candidate_account_state_sha256,
                deployment_identity_sha256,broker_snapshot_id,broker_snapshot_sha256,
                backup_id,reviewed_evidence_sha256,account_path_sha256,
                actual_account_after_sha256,reason,payload_json,payload_sha256,created_at,updated_at
            ) VALUES('direct-rebaseline','FILE_COMMITTED',1,2,?,?,?,?,?,?,?,?,?,?,
                     'reviewed','{}',?,'2026-01-06T08:00:00+00:00','2026-01-06T08:00:00+00:00')
            """,
            (
                "a" * 64,
                "b" * 64,
                "c" * 64,
                "d" * 64,
                "snapshot-1",
                "e" * 64,
                "backup-1",
                "f" * 64,
                "1" * 64,
                "2" * 64,
                "3" * 64,
            ),
        ),
        (
            "mode_transition_operations",
            """
            INSERT INTO mode_transition_operations(
                operation_id,stage,source_epoch,target_epoch,source_mode,target_mode,
                deployment_identity_sha256,backup_id,evidence_sha256,payload_json,
                payload_sha256,created_at,updated_at
            ) VALUES('direct-transition','EPOCH_COMMITTED',1,2,'PAPER','SHADOW',?,
                     'backup-1',?,'{}',?,'2026-01-06T08:00:00+00:00',
                     '2026-01-06T08:00:00+00:00')
            """,
            ("a" * 64, "b" * 64, "c" * 64),
        ),
        (
            "backup_publication_operations",
            """
            INSERT INTO backup_publication_operations(
                operation_id,backup_id,stage,reason,manifest_sha256,database_sha256,
                account_state_sha256,deployment_identity_sha256,
                operational_evidence_identity_sha256,account_authority_epoch,mode_epoch,
                bundle_name,payload_json,payload_sha256,created_at,updated_at
            ) VALUES('direct-backup','backup-1','PUBLISHED','SESSION_CLOSE',?,?,?,?,?,1,1,
                     'backup-1.fqbackup','{}',?,'2026-01-06T08:00:00+00:00',
                     '2026-01-06T08:00:00+00:00')
            """,
            tuple(value * 64 for value in "abcdef"),
        ),
        (
            "restore_operations",
            """
            INSERT INTO restore_operations(
                operation_id,restore_id,backup_id,stage,source_reason,
                source_manifest_sha256,source_database_sha256,destination_identity_sha256,
                sanitized_state_sha256,final_directory_name,deployment_identity_sha256,
                operational_evidence_identity_sha256,account_authority_epoch,mode_epoch,
                payload_json,payload_sha256,created_at,updated_at
            ) VALUES('direct-restore','restore-1','backup-1','STAGED','SESSION_CLOSE',?,?,?,
                     ?,NULL,?,?,1,1,'{}',?,'2026-01-06T08:00:00+00:00',
                     '2026-01-06T08:00:00+00:00')
            """,
            tuple(value * 64 for value in "abcdef1"),
        ),
    ],
)
def test_staged_operation_tables_reject_direct_terminal_inserts(
    tmp_path: Path, table: str, insert_sql: str, parameters: tuple[object, ...]
) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        _seed_operational_epochs(database)
        with pytest.raises(Exception, match="must start PREPARED"), database.transaction():
            database.write(insert_sql, parameters)
        assert database.scalar(f"SELECT count(*) FROM {table}") == 0
    finally:
        database.close()


def test_stage_transitions_require_completed_outputs(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        _seed_operational_epochs(database)
        with database.transaction():
            database.write(
                """
                INSERT INTO account_rebaseline_operations(
                    operation_id,stage,source_epoch,target_epoch,account_id_hash,
                    account_before_sha256,candidate_account_state_sha256,
                    deployment_identity_sha256,broker_snapshot_id,broker_snapshot_sha256,
                    backup_id,reviewed_evidence_sha256,account_path_sha256,
                    actual_account_after_sha256,reason,payload_json,payload_sha256,created_at,updated_at
                ) VALUES('rebaseline-output','PREPARED',1,2,?,?,?,?,'snapshot-1',?,
                         'backup-1',?,?,NULL,'reviewed','{}',?,
                         '2026-01-06T08:00:00+00:00','2026-01-06T08:00:00+00:00')
                """,
                (
                    "0" * 64,
                    "1" * 64,
                    "9" * 64,
                    "d" * 64,
                    "e" * 64,
                    "f" * 64,
                    "1" * 64,
                    "2" * 64,
                ),
            )
        with pytest.raises(Exception, match="actual account"), database.transaction():
            database.write(
                "UPDATE account_rebaseline_operations SET stage='FILE_COMMITTED' "
                "WHERE operation_id='rebaseline-output'"
            )

        with database.transaction():
            database.write(
                """
                INSERT INTO backup_publication_operations(
                    operation_id,backup_id,stage,reason,manifest_sha256,database_sha256,
                    account_state_sha256,deployment_identity_sha256,
                    operational_evidence_identity_sha256,account_authority_epoch,mode_epoch,
                    bundle_name,payload_json,payload_sha256,created_at,updated_at
                ) VALUES('backup-output','backup-output','PREPARED','SESSION_CLOSE',?,?,?,?,?,1,1,
                         NULL,'{}',?,'2026-01-06T08:00:00+00:00',
                         '2026-01-06T08:00:00+00:00')
                """,
                tuple(value * 64 for value in "abcdef"),
            )
        with pytest.raises(Exception, match="bundle name"), database.transaction():
            database.write(
                "UPDATE backup_publication_operations SET stage='PUBLISHED' "
                "WHERE operation_id='backup-output'"
            )

        with database.transaction():
            database.write(
                """
                INSERT INTO restore_operations(
                    operation_id,restore_id,backup_id,stage,source_reason,
                    source_manifest_sha256,source_database_sha256,destination_identity_sha256,
                    sanitized_state_sha256,final_directory_name,deployment_identity_sha256,
                    operational_evidence_identity_sha256,account_authority_epoch,mode_epoch,
                    payload_json,payload_sha256,created_at,updated_at
                ) VALUES('restore-output','restore-output','backup-output','PREPARED',
                         'SESSION_CLOSE',?,?,?,NULL,NULL,?,?,1,1,'{}',?,
                         '2026-01-06T08:00:00+00:00','2026-01-06T08:00:00+00:00')
                """,
                tuple(value * 64 for value in "abcdef"),
            )
        with pytest.raises(Exception, match="sanitized state"), database.transaction():
            database.write("UPDATE restore_operations SET stage='STAGED' WHERE operation_id='restore-output'")
        with database.transaction():
            database.write(
                "UPDATE restore_operations SET stage='STAGED',sanitized_state_sha256=? "
                "WHERE operation_id='restore-output'",
                ("9" * 64,),
            )
        with pytest.raises(Exception, match="final directory"), database.transaction():
            database.write(
                "UPDATE restore_operations SET stage='PUBLISHED' WHERE operation_id='restore-output'"
            )
    finally:
        database.close()


def test_v5_digest_and_uquant_fields_require_lowercase_hex(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        with pytest.raises(Exception, match="CHECK constraint"), database.transaction():
            database.write(
                """
                INSERT INTO mode_epochs(
                    epoch,mode,deployment_identity_sha256,payload_json,payload_sha256,created_at
                ) VALUES(1,'PAPER',NULL,'{}',?,'2026-01-06T08:00:00+00:00')
                """,
                ("g" * 64,),
            )
        with pytest.raises(Exception, match="CHECK constraint"), database.transaction():
            database.write(
                """
                INSERT INTO replay_acceptance_receipts(
                    acceptance_identity_sha256,deployment_identity_sha256,uquant_commit,
                    semantic_config_sha256,policy_sha256,universe_sha256,
                    frozen_data_manifest_sha256,normal_summary_sha256,restart_summary_sha256,
                    passed,payload_json,payload_sha256,generated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,1,'{}',?,'2026-01-06T08:00:00+00:00')
                """,
                (*("a" * 64 for _ in range(2)), "G" * 40, *("b" * 64 for _ in range(7))),
            )
        with pytest.raises(Exception, match="CHECK constraint"), database.transaction():
            database.write(
                """
                INSERT INTO mode_epochs(
                    epoch,mode,deployment_identity_sha256,caps_sha256,
                    payload_json,payload_sha256,created_at
                ) VALUES(1,'PAPER',NULL,?,'{}',?,'2026-01-06T08:00:00+00:00')
                """,
                ("g" * 64, "a" * 64),
            )
    finally:
        database.close()


def test_nullable_stage_digests_reject_nonhex_values_on_terminal_paths(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        _seed_operational_epochs(database)
        with database.transaction():
            database.write(
                """
                INSERT INTO account_rebaseline_operations(
                    operation_id,stage,source_epoch,target_epoch,account_id_hash,
                    account_before_sha256,candidate_account_state_sha256,
                    deployment_identity_sha256,broker_snapshot_id,broker_snapshot_sha256,
                    backup_id,reviewed_evidence_sha256,account_path_sha256,
                    actual_account_after_sha256,reason,payload_json,payload_sha256,created_at,updated_at
                ) VALUES('nonhex-terminal','PREPARED',1,2,?,?,?,?,'snapshot-1',?,
                         'backup-1',?,?,NULL,'reviewed','{}',?,
                         '2026-01-06T08:00:00+00:00','2026-01-06T08:00:00+00:00')
                """,
                tuple(value * 64 for value in "abcdef12"),
            )
            database.write(
                """
                INSERT INTO restore_operations(
                    operation_id,restore_id,backup_id,stage,source_reason,
                    source_manifest_sha256,source_database_sha256,destination_identity_sha256,
                    sanitized_state_sha256,final_directory_name,deployment_identity_sha256,
                    operational_evidence_identity_sha256,account_authority_epoch,mode_epoch,
                    payload_json,payload_sha256,created_at,updated_at
                ) VALUES('restore-nonhex','restore-nonhex','backup-1','PREPARED',
                         'SESSION_CLOSE',?,?,?,NULL,NULL,?,?,1,1,'{}',?,
                         '2026-01-06T08:00:00+00:00','2026-01-06T08:00:00+00:00')
                """,
                tuple(value * 64 for value in "abcdef"),
            )
        with pytest.raises(Exception, match="CHECK constraint"), database.transaction():
            database.write(
                "UPDATE account_rebaseline_operations SET stage='FILE_COMMITTED',"
                "actual_account_after_sha256=? WHERE operation_id='nonhex-terminal'",
                ("g" * 64,),
            )
        with pytest.raises(Exception, match="CHECK constraint"), database.transaction():
            database.write(
                "UPDATE restore_operations SET stage='STAGED',sanitized_state_sha256=? "
                "WHERE operation_id='restore-nonhex'",
                ("G" * 64,),
            )
    finally:
        database.close()


def _insert_published_backup_operation(database: Database, *, backup_id: str = "backup-v3") -> None:
    with database.transaction():
        database.write(
            """
            INSERT INTO backup_publication_operations(
                operation_id,backup_id,stage,reason,manifest_sha256,database_sha256,
                account_state_sha256,deployment_identity_sha256,
                operational_evidence_identity_sha256,account_authority_epoch,mode_epoch,
                bundle_name,payload_json,payload_sha256,created_at,updated_at
            ) VALUES(?,?,'PREPARED','SESSION_CLOSE',?,?,?,?,?,1,1,NULL,'{}',?,
                     '2026-01-06T08:00:00+00:00','2026-01-06T08:00:00+00:00')
            """,
            ("publish-" + backup_id, backup_id, *(value * 64 for value in "abcdef")),
        )
        database.write(
            """
            UPDATE backup_publication_operations
            SET stage='PUBLISHED',bundle_name=?,updated_at='2026-01-06T08:01:00+00:00'
            WHERE backup_id=?
            """,
            (backup_id + ".fqbackup", backup_id),
        )


def _insert_v3_backup_receipt(
    database: Database, *, backup_id: str = "backup-v3", manifest_sha256: str = "a" * 64
) -> None:
    database.write(
        """
        INSERT INTO backup_receipts(
            backup_id,database_sha256,account_state_sha256,manifest_json,manifest_sha256,
            created_at,verified_at,verification_status,bundle_schema_version,
            operational_schema_version,reason,deployment_identity_sha256,
            operational_evidence_identity_sha256,account_authority_epoch,mode_epoch,
            broker_snapshot_id,broker_snapshot_sha256
        ) VALUES(?,?,?,'{}',?,? ,?,'VERIFIED',3,5,'SESSION_CLOSE',?,?,1,1,?,?)
        """,
        (
            backup_id,
            "b" * 64,
            "c" * 64,
            manifest_sha256,
            "2026-01-06T08:01:00+00:00",
            "2026-01-06T08:01:00+00:00",
            "d" * 64,
            "e" * 64,
            "snapshot-v3",
            "9" * 64,
        ),
    )


def test_v3_backup_receipt_and_final_stage_are_mutually_bound(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        _seed_operational_epochs(database)
        with pytest.raises(Exception, match="publication operation"), database.transaction():
            _insert_v3_backup_receipt(database, backup_id="orphan")

        _insert_published_backup_operation(database)
        with pytest.raises(Exception, match="backup receipt"), database.transaction():
            database.write(
                "UPDATE backup_publication_operations SET stage='RECEIPT_COMMITTED' "
                "WHERE backup_id='backup-v3'"
            )
        with pytest.raises(Exception, match="publication operation"), database.transaction():
            _insert_v3_backup_receipt(database, manifest_sha256="0" * 64)

        with database.transaction():
            _insert_v3_backup_receipt(database)
        with pytest.raises(Exception, match="committed operation"), database.transaction():
            database.write(
                "UPDATE backup_publication_operations SET stage='CONTRADICTION' WHERE backup_id='backup-v3'"
            )
        with database.transaction():
            database.write(
                "UPDATE backup_publication_operations SET stage='RECEIPT_COMMITTED',"
                "updated_at='2026-01-06T08:02:00+00:00' WHERE backup_id='backup-v3'"
            )
        assert (
            database.scalar(
                "SELECT count(*) FROM backup_publication_operations "
                "WHERE backup_id='backup-v3' AND stage='RECEIPT_COMMITTED'"
            )
            == 1
        )
    finally:
        database.close()


def _insert_published_restore_operation(database: Database, *, restore_id: str = "restore-v1") -> None:
    with database.transaction():
        database.write(
            """
            INSERT INTO restore_operations(
                operation_id,restore_id,backup_id,stage,source_reason,
                source_manifest_sha256,source_database_sha256,destination_identity_sha256,
                sanitized_state_sha256,final_directory_name,deployment_identity_sha256,
                operational_evidence_identity_sha256,account_authority_epoch,mode_epoch,
                payload_json,payload_sha256,created_at,updated_at
            ) VALUES(?,?,?,'PREPARED','SESSION_CLOSE',?,?,?,NULL,NULL,?,?,1,1,'{}',?,
                     '2026-01-06T08:00:00+00:00','2026-01-06T08:00:00+00:00')
            """,
            (
                "operation-" + restore_id,
                restore_id,
                "backup-v3",
                *(value * 64 for value in "abcdef"),
            ),
        )
        database.write(
            "UPDATE restore_operations SET stage='STAGED',sanitized_state_sha256=?,"
            "updated_at='2026-01-06T08:01:00+00:00' WHERE restore_id=?",
            ("9" * 64, restore_id),
        )
        database.write(
            "UPDATE restore_operations SET stage='PUBLISHED',final_directory_name='restored-v1',"
            "updated_at='2026-01-06T08:02:00+00:00' WHERE restore_id=?",
            (restore_id,),
        )


def _insert_restore_receipt(
    database: Database, *, restore_id: str = "restore-v1", manifest_sha256: str = "a" * 64
) -> None:
    database.write(
        """
        INSERT INTO restore_receipts(
            restore_id,source_backup_id,source_manifest_sha256,source_reason,
            deployment_identity_sha256,operational_evidence_identity_sha256,
            account_authority_epoch,mode_epoch,source_database_sha256,sanitized_state_sha256,
            original_audit_count,original_audit_head,restored_audit_count,restored_audit_head,
            restored_at,requires_fresh_snapshot,requires_reconciliation,payload_json,payload_sha256
        ) VALUES(?, ?,?,'SESSION_CLOSE',?,?,1,1,?,?,0,?,0,?,
                 '2026-01-06T08:03:00+00:00',1,1,'{}',?)
        """,
        (
            restore_id,
            "backup-v3",
            manifest_sha256,
            "d" * 64,
            "e" * 64,
            "b" * 64,
            "9" * 64,
            "1" * 64,
            "2" * 64,
            "3" * 64,
        ),
    )


def test_restore_receipt_and_final_stage_are_mutually_bound(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        _seed_operational_epochs(database)
        with pytest.raises(Exception, match="restore operation"), database.transaction():
            _insert_restore_receipt(database, restore_id="orphan")

        _insert_published_restore_operation(database)
        with pytest.raises(Exception, match="restore receipt"), database.transaction():
            database.write(
                "UPDATE restore_operations SET stage='RECEIPT_COMMITTED' WHERE restore_id='restore-v1'"
            )
        with pytest.raises(Exception, match="restore operation"), database.transaction():
            _insert_restore_receipt(database, manifest_sha256="0" * 64)

        with database.transaction():
            _insert_restore_receipt(database)
        with pytest.raises(Exception, match="committed operation"), database.transaction():
            database.write(
                "UPDATE restore_operations SET stage='CONTRADICTION' WHERE restore_id='restore-v1'"
            )
        with database.transaction():
            database.write(
                "UPDATE restore_operations SET stage='RECEIPT_COMMITTED',"
                "updated_at='2026-01-06T08:04:00+00:00' WHERE restore_id='restore-v1'"
            )
        assert (
            database.scalar(
                "SELECT count(*) FROM restore_operations "
                "WHERE restore_id='restore-v1' AND stage='RECEIPT_COMMITTED'"
            )
            == 1
        )
    finally:
        database.close()


def test_rebaseline_receipt_and_final_stage_are_mutually_bound(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        _seed_operational_epochs(database)
        with database.transaction():
            database.write(
                """
                INSERT INTO backup_receipts(
                    backup_id,database_sha256,account_state_sha256,manifest_json,
                    manifest_sha256,created_at,verification_status
                ) VALUES('backup-bound',?,?,'{}',?,'2026-01-06T08:00:00+00:00','VERIFIED')
                """,
                ("a" * 64, "b" * 64, "c" * 64),
            )
            database.write(
                """
                INSERT INTO account_authority_epochs(
                    epoch,account_id_hash,account_state_sha256,deployment_identity_sha256,
                    source_binding_id,payload_json,payload_sha256,created_at
                ) VALUES(2,?,?,?,NULL,'{}',?,'2026-01-06T08:01:00+00:00')
                """,
                ("0" * 64, "9" * 64, "d" * 64, "8" * 64),
            )
            database.write(
                """
                INSERT INTO account_rebaseline_operations(
                    operation_id,stage,source_epoch,target_epoch,account_id_hash,
                    account_before_sha256,candidate_account_state_sha256,
                    deployment_identity_sha256,broker_snapshot_id,broker_snapshot_sha256,
                    backup_id,reviewed_evidence_sha256,account_path_sha256,
                    actual_account_after_sha256,reason,payload_json,payload_sha256,created_at,updated_at
                ) VALUES('rebaseline-bound','PREPARED',1,2,?,?,?,?,'snapshot-1',?,
                         'backup-bound',?,?,NULL,'reviewed','{}',?,
                         '2026-01-06T08:00:00+00:00','2026-01-06T08:00:00+00:00')
                """,
                (
                    "0" * 64,
                    "1" * 64,
                    "9" * 64,
                    "d" * 64,
                    "e" * 64,
                    "f" * 64,
                    "1" * 64,
                    "2" * 64,
                ),
            )
        with pytest.raises(Exception, match="rebaseline operation"), database.transaction():
            database.write(
                """
                INSERT INTO account_rebaseline_receipts(
                    receipt_id,operation_id,account_authority_epoch,deployment_identity_sha256,
                    backup_id,payload_json,payload_sha256,created_at
                ) VALUES('early','rebaseline-bound',2,?,'backup-bound','{}',?,
                         '2026-01-06T08:02:00+00:00')
                """,
                ("d" * 64, "7" * 64),
            )
        with database.transaction():
            database.write(
                "UPDATE account_rebaseline_operations SET stage='FILE_COMMITTED',"
                "actual_account_after_sha256=?,updated_at='2026-01-06T08:02:00+00:00' "
                "WHERE operation_id='rebaseline-bound'",
                ("9" * 64,),
            )
        with pytest.raises(Exception, match="rebaseline receipt"), database.transaction():
            database.write(
                "UPDATE account_rebaseline_operations SET stage='RECEIPT_COMMITTED' "
                "WHERE operation_id='rebaseline-bound'"
            )
        with pytest.raises(Exception, match="rebaseline operation"), database.transaction():
            database.write(
                """
                INSERT INTO account_rebaseline_receipts(
                    receipt_id,operation_id,account_authority_epoch,deployment_identity_sha256,
                    backup_id,payload_json,payload_sha256,created_at
                ) VALUES('mismatch','rebaseline-bound',2,?,'wrong-backup','{}',?,
                         '2026-01-06T08:02:00+00:00')
                """,
                ("d" * 64, "6" * 64),
            )
        with database.transaction():
            database.write(
                """
                INSERT INTO account_rebaseline_receipts(
                    receipt_id,operation_id,account_authority_epoch,deployment_identity_sha256,
                    backup_id,payload_json,payload_sha256,created_at
                ) VALUES('receipt-bound','rebaseline-bound',2,?,'backup-bound','{}',?,
                         '2026-01-06T08:02:00+00:00')
                """,
                ("d" * 64, "5" * 64),
            )
        with pytest.raises(Exception, match="committed operation"), database.transaction():
            database.write(
                "UPDATE account_rebaseline_operations SET stage='CONTRADICTION' "
                "WHERE operation_id='rebaseline-bound'"
            )
        with database.transaction():
            database.write(
                "UPDATE account_rebaseline_operations SET stage='RECEIPT_COMMITTED',"
                "updated_at='2026-01-06T08:03:00+00:00' WHERE operation_id='rebaseline-bound'"
            )
    finally:
        database.close()


def test_mode_transition_receipt_and_final_stage_are_mutually_bound(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        _seed_operational_epochs(database)
        with database.transaction():
            database.write(
                """
                INSERT INTO backup_receipts(
                    backup_id,database_sha256,account_state_sha256,manifest_json,
                    manifest_sha256,created_at,verification_status
                ) VALUES('backup-bound',?,?,'{}',?,'2026-01-06T08:00:00+00:00','VERIFIED')
                """,
                ("a" * 64, "b" * 64, "c" * 64),
            )
            database.write(
                """
                INSERT INTO mode_epochs(
                    epoch,mode,deployment_identity_sha256,payload_json,payload_sha256,created_at
                ) VALUES(2,'SHADOW',?,'{}',?,'2026-01-06T08:01:00+00:00')
                """,
                ("d" * 64, "8" * 64),
            )
            database.write(
                """
                INSERT INTO mode_transition_operations(
                    operation_id,stage,source_epoch,target_epoch,source_mode,target_mode,
                    deployment_identity_sha256,backup_id,evidence_sha256,payload_json,
                    payload_sha256,created_at,updated_at
                ) VALUES('transition-bound','PREPARED',1,2,'PAPER','SHADOW',?,
                         'backup-bound',?,'{}',?,'2026-01-06T08:00:00+00:00',
                         '2026-01-06T08:00:00+00:00')
                """,
                ("d" * 64, "e" * 64, "f" * 64),
            )
        with pytest.raises(Exception, match="mode transition operation"), database.transaction():
            database.write(
                """
                INSERT INTO mode_transition_receipts(
                    receipt_id,operation_id,mode_epoch,deployment_identity_sha256,
                    backup_id,payload_json,payload_sha256,created_at
                ) VALUES('early','transition-bound',2,?,'backup-bound','{}',?,
                         '2026-01-06T08:02:00+00:00')
                """,
                ("d" * 64, "7" * 64),
            )
        with database.transaction():
            database.write(
                "UPDATE mode_transition_operations SET stage='EPOCH_COMMITTED',"
                "updated_at='2026-01-06T08:02:00+00:00' WHERE operation_id='transition-bound'"
            )
        with pytest.raises(Exception, match="mode transition receipt"), database.transaction():
            database.write(
                "UPDATE mode_transition_operations SET stage='RECEIPT_COMMITTED' "
                "WHERE operation_id='transition-bound'"
            )
        with pytest.raises(Exception, match="mode transition operation"), database.transaction():
            database.write(
                """
                INSERT INTO mode_transition_receipts(
                    receipt_id,operation_id,mode_epoch,deployment_identity_sha256,
                    backup_id,payload_json,payload_sha256,created_at
                ) VALUES('mismatch','transition-bound',2,?,'wrong-backup','{}',?,
                         '2026-01-06T08:02:00+00:00')
                """,
                ("d" * 64, "6" * 64),
            )
        with database.transaction():
            database.write(
                """
                INSERT INTO mode_transition_receipts(
                    receipt_id,operation_id,mode_epoch,deployment_identity_sha256,
                    backup_id,payload_json,payload_sha256,created_at
                ) VALUES('receipt-bound','transition-bound',2,?,'backup-bound','{}',?,
                         '2026-01-06T08:02:00+00:00')
                """,
                ("d" * 64, "5" * 64),
            )
        with pytest.raises(Exception, match="committed operation"), database.transaction():
            database.write(
                "UPDATE mode_transition_operations SET stage='CONTRADICTION' "
                "WHERE operation_id='transition-bound'"
            )
        with database.transaction():
            database.write(
                "UPDATE mode_transition_operations SET stage='RECEIPT_COMMITTED',"
                "updated_at='2026-01-06T08:03:00+00:00' WHERE operation_id='transition-bound'"
            )
    finally:
        database.close()
