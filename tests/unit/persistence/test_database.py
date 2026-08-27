from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from firmquant.persistence.database import (
    Database,
    DatabaseCorrupt,
    DatabaseUnavailable,
    TransactionRequired,
)
from firmquant.persistence.schema import CURRENT_SCHEMA_VERSION


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
        assert reader.scalar("SELECT count(*) FROM schema_migrations") == CURRENT_SCHEMA_VERSION
        with pytest.raises(sqlite3.OperationalError, match="readonly"), reader.transaction():
            reader.write("DELETE FROM schema_migrations")
    finally:
        reader.close()


def test_read_only_connection_never_creates_a_missing_database(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite3"

    with pytest.raises(DatabaseUnavailable, match="read-only"):
        Database.open_read_only(path)

    assert not path.exists()


@pytest.mark.parametrize("timeout", [True, "5000", None])
@pytest.mark.parametrize("opener", [Database.open, Database.open_read_only])
def test_busy_timeout_requires_positive_integer(
    tmp_path: Path,
    timeout: object,
    opener: Callable[..., Database],
) -> None:
    path = tmp_path / "firmquant.sqlite3"
    path.touch()
    with pytest.raises(TypeError, match="integer"):
        opener(path, busy_timeout_ms=timeout)  # type: ignore[arg-type]


@pytest.mark.parametrize("timeout", [0, -1])
@pytest.mark.parametrize("opener", [Database.open, Database.open_read_only])
def test_busy_timeout_rejects_nonpositive_values(
    tmp_path: Path,
    timeout: int,
    opener: Callable[..., Database],
) -> None:
    path = tmp_path / "firmquant.sqlite3"
    path.touch()
    with pytest.raises(ValueError, match="positive"):
        opener(path, busy_timeout_ms=timeout)


@pytest.mark.parametrize("read_only", [False, True])
def test_database_symlink_is_rejected(tmp_path: Path, read_only: bool) -> None:
    target = tmp_path / "target.sqlite3"
    target.touch()
    linked = tmp_path / "linked.sqlite3"
    linked.symlink_to(target)
    opener = Database.open_read_only if read_only else Database.open
    with pytest.raises(DatabaseUnavailable, match="symbolic link"):
        opener(linked)


def test_database_parent_must_exist(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "firmquant.sqlite3"
    with pytest.raises(DatabaseUnavailable, match="parent directory"):
        Database.open(path)


@pytest.mark.parametrize(
    ("error", "exception"),
    [
        (sqlite3.DatabaseError("not a database"), DatabaseCorrupt),
        (sqlite3.DatabaseError("database is locked"), DatabaseUnavailable),
        (OSError("path unavailable"), DatabaseUnavailable),
    ],
)
def test_database_open_maps_low_level_failures_to_safe_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    exception: type[Exception],
) -> None:
    def fail_connect(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        raise error

    monkeypatch.setattr(sqlite3, "connect", fail_connect)
    with pytest.raises(exception):
        Database.open(tmp_path / "firmquant.sqlite3")


@pytest.mark.parametrize(
    ("error", "exception"),
    [
        (sqlite3.DatabaseError("malformed"), DatabaseCorrupt),
        (sqlite3.DatabaseError("permission denied"), DatabaseUnavailable),
        (OSError("path unavailable"), DatabaseUnavailable),
    ],
)
def test_read_only_open_maps_low_level_failures_to_safe_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    exception: type[Exception],
) -> None:
    path = tmp_path / "firmquant.sqlite3"
    path.touch()

    def fail_connect(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        raise error

    monkeypatch.setattr(sqlite3, "connect", fail_connect)
    with pytest.raises(exception):
        Database.open_read_only(path)


def test_database_query_helpers_and_properties(db: Database) -> None:
    assert db.path.name == "firmquant.sqlite3"
    assert db.in_transaction is False
    assert db.query_one("SELECT 1 AS value")["value"] == 1  # type: ignore[index]
    assert db.query_one("SELECT 1 WHERE 0") is None
    assert len(db.query_all("SELECT 1 UNION ALL SELECT 2")) == 2
    assert db.scalar("SELECT 1 WHERE 0") is None


def test_integrity_check_rejects_unexpected_result(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "query_all", lambda _sql: ())
    with pytest.raises(DatabaseCorrupt, match="integrity"):
        db.integrity_check()


def test_online_backup_preconditions_are_strict(db: Database, tmp_path: Path) -> None:
    destination = tmp_path / "backup.sqlite3"
    with db.transaction(), pytest.raises(TransactionRequired, match="inside a transaction"):
        db.backup_to(destination)

    destination.touch()
    with pytest.raises(DatabaseUnavailable, match="must not already exist"):
        db.backup_to(destination)

    missing_parent = tmp_path / "missing" / "backup.sqlite3"
    with pytest.raises(DatabaseUnavailable, match="parent does not exist"):
        db.backup_to(missing_parent)


def test_online_backup_creates_verified_database(db: Database, tmp_path: Path) -> None:
    destination = tmp_path / "backup.sqlite3"
    db.backup_to(destination)
    restored = Database.open_read_only(destination)
    try:
        restored.integrity_check()
        assert restored.scalar("SELECT max(version) FROM schema_migrations") == CURRENT_SCHEMA_VERSION
    finally:
        restored.close()
