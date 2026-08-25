"""Reliable single-process SQLite connection and explicit transaction boundary."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, cast


class PersistenceError(RuntimeError):
    """Base class for operational-ledger failures."""


class DatabaseUnavailable(PersistenceError):
    """Raised when SQLite cannot be opened or safely configured."""


class DatabaseCorrupt(PersistenceError):
    """Raised when SQLite integrity cannot be established."""


class TransactionRequired(PersistenceError):
    """Raised for a write without a caller-owned explicit transaction."""


class Database:
    """One SQLite connection configured for durable single-writer operation."""

    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self._path = path
        self._connection = connection

    @classmethod
    def open(cls, path: Path, *, busy_timeout_ms: int = 5_000) -> Database:
        """Open, verify, migrate, and return a fail-closed SQLite ledger."""

        if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int):
            raise TypeError("busy timeout must be an integer")
        if busy_timeout_ms <= 0:
            raise ValueError("busy timeout must be positive")
        database_path = Path(path)
        if database_path.is_symlink():
            raise DatabaseUnavailable("database path must not be a symbolic link")
        if not database_path.parent.is_dir():
            raise DatabaseUnavailable("database parent directory does not exist")
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                database_path,
                timeout=busy_timeout_ms / 1_000,
                isolation_level=None,
                check_same_thread=True,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA recursive_triggers = ON")
            connection.execute("PRAGMA temp_store = MEMORY")
            check = connection.execute("PRAGMA quick_check").fetchone()
            if check is None or check[0] != "ok":
                raise DatabaseCorrupt("database integrity check did not return ok")
        except DatabaseCorrupt:
            if connection is not None:
                connection.close()
            raise
        except sqlite3.DatabaseError as exc:
            if connection is not None:
                connection.close()
            if "malformed" in str(exc).lower() or "not a database" in str(exc).lower():
                raise DatabaseCorrupt("database integrity/configuration check failed") from exc
            raise DatabaseUnavailable("cannot open or configure SQLite database") from exc
        except OSError as exc:
            if connection is not None:
                connection.close()
            raise DatabaseUnavailable("cannot open SQLite database path") from exc

        if connection is None:
            raise DatabaseUnavailable("SQLite connection was not created")
        database = cls(database_path, connection)
        try:
            from .schema import apply_migrations

            apply_migrations(database)
            foreign_key_errors = database.query_all("PRAGMA foreign_key_check")
            if foreign_key_errors:
                raise DatabaseCorrupt("database foreign-key integrity check failed")
            if os.name != "nt":
                database_path.chmod(0o600)
        except Exception:
            database.close()
            raise
        return database

    @property
    def path(self) -> Path:
        return self._path

    @property
    def in_transaction(self) -> bool:
        return self._connection.in_transaction

    def close(self) -> None:
        self._connection.close()

    def scalar(self, sql: str, parameters: Sequence[object] = ()) -> object | None:
        row = self._connection.execute(sql, tuple(parameters)).fetchone()
        return None if row is None else row[0]

    def query_one(
        self,
        sql: str,
        parameters: Sequence[object] = (),
    ) -> sqlite3.Row | None:
        row = self._connection.execute(sql, tuple(parameters)).fetchone()
        return cast(sqlite3.Row | None, row)

    def query_all(
        self,
        sql: str,
        parameters: Sequence[object] = (),
    ) -> tuple[sqlite3.Row, ...]:
        return tuple(self._connection.execute(sql, tuple(parameters)).fetchall())

    def write(self, sql: str, parameters: Sequence[object] = ()) -> sqlite3.Cursor:
        """Execute one statement only inside a caller-owned explicit transaction."""

        if not self._connection.in_transaction:
            raise TransactionRequired("SQLite write requires an explicit transaction")
        return self._connection.execute(sql, tuple(parameters))

    @contextmanager
    def transaction(
        self,
        mode: Literal["DEFERRED", "IMMEDIATE", "EXCLUSIVE"] = "IMMEDIATE",
    ) -> Iterator[None]:
        """Commit one atomic unit or roll it back on every exception."""

        if self._connection.in_transaction:
            raise TransactionRequired("nested SQLite transactions are forbidden")
        self._connection.execute(f"BEGIN {mode}")
        try:
            yield
        except BaseException:
            self._connection.rollback()
            raise
        else:
            try:
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    def integrity_check(self) -> None:
        rows = self.query_all("PRAGMA integrity_check")
        if len(rows) != 1 or rows[0][0] != "ok":
            raise DatabaseCorrupt("database integrity check failed")


__all__ = (
    "Database",
    "DatabaseCorrupt",
    "DatabaseUnavailable",
    "PersistenceError",
    "TransactionRequired",
)
