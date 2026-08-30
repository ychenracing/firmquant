from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from firmquant.persistence.database import Database
from firmquant.persistence.repositories import (
    PersistenceConflict,
    Repositories,
    canonical_json,
)
from firmquant.persistence.schema import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    Migration,
    MigrationError,
    apply_migrations,
)

REQUIRED_TABLES = {
    "runtime_state",
    "arm_leases",
    "decision_snapshots",
    "execution_intents",
    "broker_orders",
    "broker_order_attempts",
    "order_commands",
    "broker_responses",
    "broker_events",
    "domain_events",
    "fills",
    "broker_snapshots",
    "position_snapshots",
    "cash_snapshots",
    "reconciliation_runs",
    "risk_events",
    "alerts",
    "audit_events",
    "writer_leases",
    "backup_receipts",
    "account_operations",
    "account_authority_epochs",
    "account_authority_active",
    "mode_epochs",
    "mode_epoch_active",
    "deployment_identities",
    "operational_evidence_receipts",
    "account_rebaseline_operations",
    "account_rebaseline_receipts",
    "mode_transition_operations",
    "mode_transition_receipts",
    "backup_publication_operations",
    "restore_operations",
    "restore_receipts",
    "replay_acceptance_receipts",
    "schema_migrations",
}


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        yield database
    finally:
        database.close()


def test_initial_migration_contains_complete_operational_ledger(db: Database) -> None:
    tables = {
        str(row[0])
        for row in db.query_all(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }

    assert tables >= REQUIRED_TABLES
    assert db.scalar("SELECT max(version) FROM schema_migrations") == CURRENT_SCHEMA_VERSION


def test_migrations_are_repeatable_without_rewriting_receipts(db: Database) -> None:
    before = db.query_all(
        "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version"
    )
    apply_migrations(db)

    assert (
        db.query_all("SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version")
        == before
    )


def _v4_database(path: Path) -> Database:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    database = Database(path, connection)
    apply_migrations(database, migrations=MIGRATIONS[:4])
    return database


def test_v5_migration_seeds_only_real_legacy_binding_and_mode(tmp_path: Path) -> None:
    database = _v4_database(tmp_path / "legacy.sqlite3")
    binding_payload = canonical_json(
        {
            "schema": "firmquant.account-binding.v1",
            "account_id_hash": "a" * 64,
            "account_state_sha256": "c" * 64,
            "created_at": "2026-01-06T08:05:00+00:00",
        }
    )
    binding_sha256 = hashlib.sha256(binding_payload.encode()).hexdigest()
    try:
        with database.transaction():
            database.write(
                """
                INSERT INTO account_bindings(
                    binding_id,singleton_id,account_id_hash,account_type,
                    broker_snapshot_sha256,account_state_sha256,uquant_commit,
                    uquant_code_fingerprint,data_hash,data_as_of,data_symbols_json,
                    payload_json,payload_sha256,created_at
                ) VALUES(?,1,?,'CASH',?,?,?,?,?,?,'[]',?,?,?)
                """,
                (
                    "acctbind_" + binding_sha256,
                    "a" * 64,
                    "b" * 64,
                    "c" * 64,
                    "1" * 40,
                    "d" * 64,
                    "e" * 64,
                    "2026-01-05",
                    binding_payload,
                    binding_sha256,
                    "2026-01-06T08:05:00+00:00",
                ),
            )
            database.write(
                """
                INSERT INTO runtime_state(
                    singleton_id,mode,state,revision,reason,blockers_json,updated_at
                ) VALUES(1,'SHADOW','DISARMED',0,'legacy','[]','2026-01-06T08:06:00+00:00')
                """
            )

        apply_migrations(database)

        assert tuple(
            database.query_one(
                "SELECT epoch,account_id_hash,account_state_sha256,deployment_identity_sha256 "
                "FROM account_authority_epochs"
            )
            or ()
        ) == (1, "a" * 64, "c" * 64, None)
        assert tuple(database.query_one("SELECT singleton_id,epoch FROM account_authority_active") or ()) == (
            1,
            1,
        )
        assert tuple(
            database.query_one("SELECT epoch,mode,deployment_identity_sha256 FROM mode_epochs") or ()
        ) == (1, "SHADOW", None)
        assert tuple(database.query_one("SELECT singleton_id,epoch FROM mode_epoch_active") or ()) == (1, 1)
    finally:
        database.close()


def test_v5_migration_does_not_fabricate_missing_legacy_facts(tmp_path: Path) -> None:
    database = _v4_database(tmp_path / "empty-v4.sqlite3")
    try:
        apply_migrations(database)
        assert database.scalar("SELECT count(*) FROM account_authority_epochs") == 0
        assert database.scalar("SELECT count(*) FROM account_authority_active") == 0
        assert database.scalar("SELECT count(*) FROM mode_epochs") == 0
        assert database.scalar("SELECT count(*) FROM mode_epoch_active") == 0
    finally:
        database.close()


def test_v5_reserves_complete_backup_restore_and_replay_fields(db: Database) -> None:
    def columns(table: str) -> set[str]:
        return {str(row["name"]) for row in db.query_all(f"PRAGMA table_info({table})")}

    assert {
        "bundle_schema_version",
        "operational_schema_version",
        "reason",
        "deployment_identity_sha256",
        "operational_evidence_identity_sha256",
        "account_authority_epoch",
        "mode_epoch",
        "broker_snapshot_id",
        "broker_snapshot_sha256",
    } <= columns("backup_receipts")
    assert {
        "operation_id",
        "backup_id",
        "stage",
        "reason",
        "manifest_sha256",
        "database_sha256",
        "account_state_sha256",
        "deployment_identity_sha256",
        "operational_evidence_identity_sha256",
        "account_authority_epoch",
        "mode_epoch",
        "bundle_name",
        "payload_json",
        "payload_sha256",
        "created_at",
        "updated_at",
    } <= columns("backup_publication_operations")
    assert {
        "operation_id",
        "restore_id",
        "backup_id",
        "stage",
        "source_manifest_sha256",
        "source_database_sha256",
        "destination_identity_sha256",
        "sanitized_state_sha256",
        "final_directory_name",
        "deployment_identity_sha256",
        "operational_evidence_identity_sha256",
        "account_authority_epoch",
        "mode_epoch",
        "payload_json",
        "payload_sha256",
        "created_at",
        "updated_at",
    } <= columns("restore_operations")
    assert {
        "restore_id",
        "source_backup_id",
        "source_manifest_sha256",
        "source_reason",
        "deployment_identity_sha256",
        "operational_evidence_identity_sha256",
        "account_authority_epoch",
        "mode_epoch",
        "source_database_sha256",
        "sanitized_state_sha256",
        "original_audit_count",
        "original_audit_head",
        "restored_audit_count",
        "restored_audit_head",
        "restored_at",
        "requires_fresh_snapshot",
        "requires_reconciliation",
        "payload_json",
        "payload_sha256",
    } <= columns("restore_receipts")
    assert {
        "acceptance_identity_sha256",
        "deployment_identity_sha256",
        "uquant_commit",
        "semantic_config_sha256",
        "policy_sha256",
        "universe_sha256",
        "frozen_data_manifest_sha256",
        "normal_summary_sha256",
        "restart_summary_sha256",
        "passed",
        "payload_json",
        "payload_sha256",
        "generated_at",
    } <= columns("replay_acceptance_receipts")
    assert "deployment_identity_sha256" in columns("deployment_identities")
    assert "account_id_hash" in columns("deployment_identities")
    assert {
        "account_state_sha256",
        "strategy_session",
        "phase",
        "kind",
    } <= columns("operational_evidence_receipts")

    evidence_indexes = {
        str(row["name"]): tuple(
            str(column["name"]) for column in db.query_all(f"PRAGMA index_info({row['name']})")
        )
        for row in db.query_all("PRAGMA index_list(operational_evidence_receipts)")
    }
    assert evidence_indexes["operational_evidence_current_lookup_idx"] == (
        "deployment_identity_sha256",
        "account_authority_epoch",
        "mode_epoch",
        "phase",
        "kind",
    )
    replay_unique_indexes = {
        tuple(str(column["name"]) for column in db.query_all(f"PRAGMA index_info({row['name']})"))
        for row in db.query_all("PRAGMA index_list(replay_acceptance_receipts)")
        if int(row["unique"]) == 1
    }
    assert (
        "deployment_identity_sha256",
        "uquant_commit",
        "semantic_config_sha256",
        "policy_sha256",
        "universe_sha256",
        "frozen_data_manifest_sha256",
    ) in replay_unique_indexes


def test_failed_migration_is_fully_rolled_back(db: Database) -> None:
    failing = Migration.create(
        version=CURRENT_SCHEMA_VERSION + 1,
        name="injected_failure",
        statements=(
            "CREATE TABLE must_rollback (value INTEGER NOT NULL) STRICT",
            "INSERT INTO table_that_does_not_exist(value) VALUES (1)",
        ),
    )

    with pytest.raises(MigrationError, match="injected_failure"):
        apply_migrations(db, migrations=(*MIGRATIONS, failing))

    assert (
        db.scalar("SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = 'must_rollback'") == 0
    )
    assert db.scalar("SELECT max(version) FROM schema_migrations") == CURRENT_SCHEMA_VERSION


def test_v5_checksum_mismatch_fails_closed_without_applying_schema(tmp_path: Path) -> None:
    database = _v4_database(tmp_path / "checksum.sqlite3")
    try:
        with database.transaction():
            database.write(
                """
                INSERT INTO schema_migrations(version,name,checksum,applied_at)
                VALUES(5,'operational_authority_epochs',?,'2026-08-30T08:00:00+00:00')
                """,
                ("0" * 64,),
            )
        with pytest.raises(MigrationError, match="receipt mismatch at version 5"):
            apply_migrations(database)
        assert (
            database.scalar("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='mode_epochs'")
            == 0
        )
    finally:
        database.close()


def test_future_migration_receipt_fails_closed(db: Database) -> None:
    with db.transaction():
        db.write(
            """
            INSERT INTO schema_migrations(version,name,checksum,applied_at)
            VALUES(6,'unknown_future',?,'2026-08-30T08:00:00+00:00')
            """,
            ("0" * 64,),
        )
    with pytest.raises(MigrationError, match="version 6 is newer"):
        apply_migrations(db)


def test_raw_broker_event_is_append_only_and_idempotent(db: Database) -> None:
    repository = Repositories.bind(db).broker_events
    arguments = {
        "broker_event_id": "broker-event-1",
        "event_type": "ORDER_UPDATE",
        "broker_sequence": 7,
        "session_date": date(2026, 8, 25),
        "event_time": datetime(2026, 8, 25, 1, tzinfo=UTC),
        "received_at": datetime(2026, 8, 25, 1, 0, 1, tzinfo=UTC),
        "safe_payload": {"price": "100.01", "symbol": "sz300308"},
        "raw_payload_sha256": "a" * 64,
    }
    with db.transaction():
        assert repository.append(**arguments) is True
    with db.transaction():
        assert repository.append(**arguments) is False
    with db.transaction(), pytest.raises(PersistenceConflict, match="broker event identity collision"):
        repository.append(**{**arguments, "safe_payload": {"symbol": "sh600000"}})

    with pytest.raises(sqlite3.IntegrityError, match="append-only"), db.transaction():
        db.write(
            "UPDATE broker_events SET event_type = ? WHERE broker_event_id = ?",
            ("MUTATED", "broker-event-1"),
        )


def test_canonical_json_rejects_binary_float_and_is_stable() -> None:
    assert canonical_json({"b": [2, 1], "a": "x"}) == '{"a":"x","b":[2,1]}'
    with pytest.raises(TypeError, match="binary float"):
        canonical_json({"price": 10.1})
