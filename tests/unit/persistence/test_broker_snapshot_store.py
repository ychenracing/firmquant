from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from firmquant.persistence.broker_snapshot_store import BrokerSnapshotStore
from firmquant.persistence.database import Database
from tests.fixtures.session_cases import execution_snapshot


def test_snapshot_store_is_idempotent_and_retains_previous_account_identity(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    store = BrokerSnapshotStore(database)
    snapshot = execution_snapshot().broker_snapshot
    try:
        assert store.previous_account_identity(snapshot) == (
            snapshot.account.account_id_hash,
            snapshot.account.account_type,
        )
        assert store.persist(snapshot) is True
        assert store.persist(snapshot) is False
        assert store.previous_account_identity(snapshot) == (
            snapshot.account.account_id_hash,
            snapshot.account.account_type,
        )
        assert database.scalar("SELECT count(*) FROM broker_snapshots") == 1
        assert database.scalar("SELECT count(*) FROM cash_snapshots") == 1
        assert database.scalar("SELECT count(*) FROM position_snapshots") == len(snapshot.positions)
        stored = store.latest()
        assert stored is not None
        assert store.load(snapshot.snapshot_id) == stored
        assert store.load("missing-snapshot") is None
        assert store.load("' OR 1=1 --") is None
        assert stored.started_at is None
        assert stored.completed_at is None
        assert stored.duration_ms is None
    finally:
        database.close()


def test_snapshot_store_persists_independent_monotonic_timing(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    store = BrokerSnapshotStore(database)
    snapshot = execution_snapshot().broker_snapshot
    started_at = datetime(2026, 8, 25, 1, 30, tzinfo=UTC)
    completed_at = started_at + timedelta(seconds=3)
    try:
        assert store.persist(
            snapshot,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=41,
        )
        stored = store.latest()
        assert stored is not None
        assert stored.started_at == started_at
        assert stored.completed_at == completed_at
        assert stored.duration_ms == 41
    finally:
        database.close()


def test_snapshot_store_normalizes_aware_captured_at_without_creating_unreadable_rows(
    tmp_path: Path,
) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    store = BrokerSnapshotStore(database)
    snapshot = replace(
        execution_snapshot().broker_snapshot,
        captured_at=execution_snapshot().broker_snapshot.captured_at.astimezone(timezone(timedelta(hours=8))),
    )
    try:
        assert store.persist(snapshot) is True
        assert store.persist(snapshot) is False
        stored = store.load(snapshot.snapshot_id)
        assert stored is not None
        assert stored.captured_at == snapshot.captured_at.astimezone(UTC)
        assert stored.captured_at.tzinfo is UTC
    finally:
        database.close()


def test_snapshot_latest_orders_legacy_mixed_offsets_by_instant(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    store = BrokerSnapshotStore(database)
    base = execution_snapshot().broker_snapshot
    older = replace(
        base,
        snapshot_id="legacy-offset-older",
        captured_at=datetime(2026, 8, 25, 10, 0, tzinfo=timezone(timedelta(hours=8))),
    )
    newer = replace(
        base,
        snapshot_id="utc-newer",
        captured_at=datetime(2026, 8, 25, 3, 0, tzinfo=UTC),
        raw_payload_sha256="7" * 64,
    )
    try:
        assert store.persist(older)
        assert store.persist(newer)
        with database.transaction():
            database.write("DROP TRIGGER broker_snapshots_reject_update")
            database.write(
                "UPDATE broker_snapshots SET captured_at=? WHERE snapshot_id=?",
                (older.captured_at.isoformat(), older.snapshot_id),
            )

        stored_older = store.load(older.snapshot_id)
        assert stored_older is not None
        assert stored_older.captured_at == older.captured_at.astimezone(UTC)
        assert store.persist(older) is False
        latest = store.latest()
        assert latest is not None
        assert latest.snapshot_id == newer.snapshot_id
    finally:
        database.close()


@pytest.mark.parametrize(
    ("started_at", "completed_at", "duration_ms", "message"),
    [
        (datetime(2026, 8, 25, 1, 30, tzinfo=UTC), None, None, "all null or all set"),
        (
            datetime(2026, 8, 25, 9, 30, tzinfo=timezone(timedelta(hours=8))),
            datetime(2026, 8, 25, 9, 31, tzinfo=timezone(timedelta(hours=8))),
            1,
            "UTC",
        ),
        (
            datetime(2026, 8, 25, 1, 31, tzinfo=UTC),
            datetime(2026, 8, 25, 1, 30, tzinfo=UTC),
            1,
            "precedes",
        ),
        (
            datetime(2026, 8, 25, 1, 30, tzinfo=UTC),
            datetime(2026, 8, 25, 1, 31, tzinfo=UTC),
            -1,
            "nonnegative",
        ),
    ],
)
def test_snapshot_store_rejects_malformed_timing(
    tmp_path: Path,
    started_at: datetime | None,
    completed_at: datetime | None,
    duration_ms: int | None,
    message: str,
) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        with pytest.raises((TypeError, ValueError), match=message):
            BrokerSnapshotStore(database).persist(
                execution_snapshot().broker_snapshot,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration_ms,
            )
    finally:
        database.close()


def test_snapshot_timing_participates_in_idempotency_identity(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    store = BrokerSnapshotStore(database)
    snapshot = execution_snapshot().broker_snapshot
    started_at = datetime(2026, 8, 25, 1, 30, tzinfo=UTC)
    completed_at = started_at + timedelta(seconds=1)
    try:
        assert store.persist(snapshot, started_at=started_at, completed_at=completed_at, duration_ms=10)
        with pytest.raises(RuntimeError, match="identity collision"):
            store.persist(
                replace(snapshot),
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=11,
            )
    finally:
        database.close()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("captured_at", "2026-08-25T01:30:00Z"),
        ("account_id_hash", "g" * 64),
        ("raw_payload_sha256", "A" * 64),
        ("broker_event_watermark", -1),
        ("complete", 0),
        ("started_at", "2026-08-25T01:30:00Z"),
        ("duration_ms", -1),
    ],
)
def test_snapshot_reads_reject_malformed_parent_rows(tmp_path: Path, column: str, value: object) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    store = BrokerSnapshotStore(database)
    snapshot = execution_snapshot().broker_snapshot
    started_at = datetime(2026, 8, 25, 1, 30, tzinfo=UTC)
    try:
        assert store.persist(
            snapshot,
            started_at=started_at,
            completed_at=started_at + timedelta(seconds=1),
            duration_ms=7,
        )
        with database.transaction():
            database.write("DROP TRIGGER broker_snapshots_reject_update")
        database.scalar("PRAGMA ignore_check_constraints = ON")
        with database.transaction():
            database.write(
                f"UPDATE broker_snapshots SET {column}=? WHERE snapshot_id=?",
                (value, snapshot.snapshot_id),
            )
        database.scalar("PRAGMA ignore_check_constraints = OFF")

        with pytest.raises(RuntimeError, match="stored broker snapshot is invalid"):
            store.load(snapshot.snapshot_id)
        with pytest.raises(RuntimeError, match="stored broker snapshot is invalid"):
            store.latest()
    finally:
        database.close()


def test_snapshot_reads_require_cash_child(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    store = BrokerSnapshotStore(database)
    snapshot = execution_snapshot().broker_snapshot
    try:
        assert store.persist(snapshot)
        with database.transaction():
            database.write("DROP TRIGGER cash_snapshots_reject_delete")
            database.write("DELETE FROM cash_snapshots WHERE snapshot_id=?", (snapshot.snapshot_id,))

        with pytest.raises(RuntimeError, match="stored broker snapshot is invalid"):
            store.load(snapshot.snapshot_id)
        with pytest.raises(RuntimeError, match="stored broker snapshot is invalid"):
            store.latest()
    finally:
        database.close()
