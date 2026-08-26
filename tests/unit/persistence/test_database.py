from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from firmquant.persistence.database import (
    Database,
    DatabaseCorrupt,
    DatabaseUnavailable,
    TransactionRequired,
)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        yield database
    finally:
        database.close()


def test_database_enables_safety_pragmas(db: Database) -> None:
    assert db.scalar("PRAGMA journal_mode") == "wal"
    assert db.scalar("PRAGMA foreign_keys") == 1
    assert db.scalar("PRAGMA synchronous") == 2
    assert db.scalar("PRAGMA busy_timeout") == 5_000
    assert db.scalar("PRAGMA trusted_schema") == 0


def test_explicit_transaction_rolls_back_ddl_and_data(db: Database) -> None:
    with pytest.raises(RuntimeError, match="injected failure"), db.transaction():
        db.write("CREATE TABLE rollback_probe (value INTEGER NOT NULL) STRICT")
        db.write("INSERT INTO rollback_probe(value) VALUES (?)", (1,))
        raise RuntimeError("injected failure")

    assert (
        db.scalar("SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = 'rollback_probe'") == 0
    )


def test_write_requires_explicit_transaction(db: Database) -> None:
    with pytest.raises(TransactionRequired, match="explicit transaction"):
        db.write("CREATE TABLE unsafe_write (value INTEGER)")


def test_nested_transactions_are_rejected(db: Database) -> None:
    with (
        db.transaction(),
        pytest.raises(TransactionRequired, match="nested"),
        db.transaction(),
    ):
        pass


def test_corrupt_database_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.sqlite3"
    path.write_bytes(b"not a SQLite database")

    with pytest.raises(DatabaseCorrupt, match="integrity"):
        Database.open(path)


def test_closed_database_cannot_be_reused(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "closed.sqlite3")
    database.close()

    with pytest.raises(sqlite3.ProgrammingError):
        database.scalar("SELECT 1")


def test_read_only_connection_verifies_existing_ledger_and_rejects_writes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "firmquant.sqlite3"
    writer = Database.open(path)
    writer.close()

    reader = Database.open_read_only(path)
    try:
        assert reader.scalar("PRAGMA query_only") == 1
        assert reader.scalar("PRAGMA foreign_keys") == 1
        assert reader.scalar("SELECT count(*) FROM schema_migrations") == 2
        with pytest.raises(sqlite3.OperationalError, match="readonly"), reader.transaction():
            reader.write("DELETE FROM schema_migrations")
    finally:
        reader.close()


def test_read_only_connection_never_creates_a_missing_database(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite3"

    with pytest.raises(DatabaseUnavailable, match="read-only"):
        Database.open_read_only(path)

    assert not path.exists()
