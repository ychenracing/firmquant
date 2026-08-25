"""Cross-platform OS lock plus expiring SQLite account-writer lease."""

from __future__ import annotations

import hashlib
import importlib
import os
import socket
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import BinaryIO, Protocol, cast

from .database import Database, PersistenceError


class WriterLeaseBusy(PersistenceError):
    """Raised when another firmquant instance owns the writer lease."""


class WriterLeaseLost(PersistenceError):
    """Raised when renewal no longer matches the acquired generation."""


class _FcntlContract(Protocol):
    LOCK_EX: int
    LOCK_NB: int
    LOCK_UN: int

    def flock(self, file_descriptor: int, operation: int) -> None: ...


class _MsvcrtContract(Protocol):
    LK_NBLCK: int
    LK_UNLCK: int

    def locking(self, file_descriptor: int, mode: int, byte_count: int) -> None: ...


def _module(name: str) -> ModuleType:
    return importlib.import_module(name)


def _acquire_file_lock(handle: BinaryIO) -> str:
    try:
        if os.name == "nt":
            msvcrt_module = cast(_MsvcrtContract, _module("msvcrt"))
            handle.seek(0)
            msvcrt_module.locking(handle.fileno(), msvcrt_module.LK_NBLCK, 1)
            return "msvcrt"
        fcntl_module = cast(_FcntlContract, _module("fcntl"))
        fcntl_module.flock(
            handle.fileno(), fcntl_module.LOCK_EX | fcntl_module.LOCK_NB
        )
        return "fcntl"
    except OSError as exc:
        raise WriterLeaseBusy("account writer lease is already held by another instance") from exc


def _release_file_lock(handle: BinaryIO, mechanism: str) -> None:
    if mechanism == "msvcrt":
        msvcrt_module = cast(_MsvcrtContract, _module("msvcrt"))
        handle.seek(0)
        msvcrt_module.locking(handle.fileno(), msvcrt_module.LK_UNLCK, 1)
    else:
        fcntl_module = cast(_FcntlContract, _module("fcntl"))
        fcntl_module.flock(handle.fileno(), fcntl_module.LOCK_UN)


def _aware_now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("writer lease clock must return timezone-aware datetime")
    return value


class WriterLease:
    """Exclusive writer ownership that is lost unless periodically renewed."""

    def __init__(
        self,
        *,
        database: Database,
        lock_handle: BinaryIO,
        lock_mechanism: str,
        owner: str,
        host_hash: str,
        process_id: int,
        generation: int,
        expires_at: datetime,
        ttl: timedelta,
        clock: Callable[[], datetime],
    ) -> None:
        self.database = database
        self._lock_handle = lock_handle
        self._lock_mechanism = lock_mechanism
        self.owner = owner
        self.host_hash = host_hash
        self.process_id = process_id
        self.generation = generation
        self.expires_at = expires_at
        self._ttl = ttl
        self._clock = clock
        self._active = True

    @classmethod
    def acquire(
        cls,
        database_path: Path,
        *,
        owner: str,
        ttl: timedelta = timedelta(seconds=30),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> WriterLease:
        if not isinstance(owner, str) or not owner or owner != owner.strip() or len(owner) > 128:
            raise ValueError("writer lease owner must be canonical non-empty text")
        if not isinstance(ttl, timedelta) or not timedelta(seconds=5) <= ttl <= timedelta(minutes=5):
            raise ValueError("writer lease TTL must be between 5 seconds and 5 minutes")
        path = Path(database_path)
        if not path.parent.is_dir():
            raise ValueError("writer lease database parent does not exist")
        lock_path = path.with_suffix(path.suffix + ".writer.lock")
        lock_handle = lock_path.open("a+b")
        try:
            if lock_path.stat().st_size == 0:
                lock_handle.write(b"\0")
                lock_handle.flush()
                os.fsync(lock_handle.fileno())
            if os.name != "nt":
                lock_path.chmod(0o600)
            lock_mechanism = _acquire_file_lock(lock_handle)
        except BaseException:
            lock_handle.close()
            raise

        database: Database | None = None
        try:
            database = Database.open(path)
            now = _aware_now(clock)
            host_hash = hashlib.sha256(socket.gethostname().encode("utf-8")).hexdigest()
            process_id = os.getpid()
            with database.transaction("EXCLUSIVE"):
                existing = database.query_one(
                    """
                    SELECT owner_id, expires_at, generation FROM writer_leases
                    WHERE singleton_id = 1
                    """
                )
                if existing is None:
                    generation = 1
                    database.write(
                        """
                        INSERT INTO writer_leases(
                            singleton_id, owner_id, host_hash, process_id, acquired_at,
                            renewed_at, expires_at, generation
                        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            owner,
                            host_hash,
                            process_id,
                            now.isoformat(),
                            now.isoformat(),
                            (now + ttl).isoformat(),
                            generation,
                        ),
                    )
                else:
                    observed_expiry = datetime.fromisoformat(str(existing["expires_at"]))
                    if observed_expiry.tzinfo is None or observed_expiry.utcoffset() is None:
                        raise WriterLeaseLost("stored writer lease expiry is not timezone-aware")
                    if observed_expiry > now:
                        raise WriterLeaseBusy("database writer lease has not expired")
                    generation = int(existing["generation"]) + 1
                    database.write(
                        """
                        UPDATE writer_leases SET
                            owner_id = ?, host_hash = ?, process_id = ?, acquired_at = ?,
                            renewed_at = ?, expires_at = ?, generation = ?
                        WHERE singleton_id = 1
                        """,
                        (
                            owner,
                            host_hash,
                            process_id,
                            now.isoformat(),
                            now.isoformat(),
                            (now + ttl).isoformat(),
                            generation,
                        ),
                    )
            return cls(
                database=database,
                lock_handle=lock_handle,
                lock_mechanism=lock_mechanism,
                owner=owner,
                host_hash=host_hash,
                process_id=process_id,
                generation=generation,
                expires_at=now + ttl,
                ttl=ttl,
                clock=clock,
            )
        except BaseException:
            if database is not None:
                database.close()
            _release_file_lock(lock_handle, lock_mechanism)
            lock_handle.close()
            raise

    @property
    def active(self) -> bool:
        return self._active

    def renew(self) -> None:
        if not self._active:
            raise WriterLeaseLost("writer lease is no longer active")
        now = _aware_now(self._clock)
        expires_at = now + self._ttl
        with self.database.transaction("EXCLUSIVE"):
            cursor = self.database.write(
                """
                UPDATE writer_leases SET renewed_at = ?, expires_at = ?
                WHERE singleton_id = 1 AND owner_id = ? AND host_hash = ?
                  AND process_id = ? AND generation = ?
                """,
                (
                    now.isoformat(),
                    expires_at.isoformat(),
                    self.owner,
                    self.host_hash,
                    self.process_id,
                    self.generation,
                ),
            )
            if cursor.rowcount != 1:
                raise WriterLeaseLost("writer lease generation or owner changed")
        self.expires_at = expires_at

    def release(self, *, remove_database_lease: bool = True) -> None:
        if not self._active:
            return
        try:
            if remove_database_lease:
                with self.database.transaction("EXCLUSIVE"):
                    self.database.write(
                        """
                        DELETE FROM writer_leases
                        WHERE singleton_id = 1 AND owner_id = ? AND host_hash = ?
                          AND process_id = ? AND generation = ?
                        """,
                        (self.owner, self.host_hash, self.process_id, self.generation),
                    )
        finally:
            self.database.close()
            try:
                _release_file_lock(self._lock_handle, self._lock_mechanism)
            finally:
                self._lock_handle.close()
                self._active = False

    def __enter__(self) -> WriterLease:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        del exc_type, exc_value, traceback
        self.release()


__all__ = ("WriterLease", "WriterLeaseBusy", "WriterLeaseLost")
