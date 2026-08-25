from __future__ import annotations

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

    assert db.query_all(
        "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version"
    ) == before


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
        db.scalar(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = 'must_rollback'"
        )
        == 0
    )
    assert db.scalar("SELECT max(version) FROM schema_migrations") == CURRENT_SCHEMA_VERSION


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
    with db.transaction(), pytest.raises(
        PersistenceConflict, match="broker event identity collision"
    ):
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
