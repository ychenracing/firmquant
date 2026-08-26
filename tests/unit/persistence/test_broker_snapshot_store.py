from __future__ import annotations

from pathlib import Path

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
    finally:
        database.close()
