from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from firmquant.persistence.database import Database
from firmquant.persistence.writer_lease import WriterLease, WriterLeaseBusy


def test_second_writer_is_rejected_and_release_allows_takeover(tmp_path: Path) -> None:
    db_path = tmp_path / "firmquant.sqlite3"
    first = WriterLease.acquire(db_path, owner="one")
    try:
        with pytest.raises(WriterLeaseBusy, match="writer lease"):
            WriterLease.acquire(db_path, owner="two")
    finally:
        first.release()

    second = WriterLease.acquire(db_path, owner="two")
    second.release()


def test_writer_lease_renewal_advances_expiry_and_generation_is_stable(tmp_path: Path) -> None:
    now = datetime(2026, 8, 25, 1, tzinfo=UTC)
    current = now

    def clock() -> datetime:
        return current

    lease = WriterLease.acquire(
        tmp_path / "firmquant.sqlite3",
        owner="one",
        ttl=timedelta(seconds=30),
        clock=clock,
    )
    try:
        original_expiry = lease.expires_at
        current = now + timedelta(seconds=10)
        lease.renew()

        assert lease.expires_at > original_expiry
        assert lease.generation == 1
    finally:
        lease.release()


def test_expired_database_lease_is_recovered_only_after_os_lock_is_available(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "firmquant.sqlite3"
    seed = WriterLease.acquire(db_path, owner="seed")
    seed.release(remove_database_lease=False)
    database = Database.open(db_path)
    with database.transaction():
        database.write(
            "UPDATE writer_leases SET expires_at = ? WHERE singleton_id = 1",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
        )
    database.close()

    recovered = WriterLease.acquire(db_path, owner="recovered")
    try:
        assert recovered.generation == 2
    finally:
        recovered.release()
