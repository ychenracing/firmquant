from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from firmquant.persistence.database import Database
from firmquant.persistence.recovery import UquantAccountStateStore
from firmquant.strategy.identity import StrategyIdentity
from firmquant.strategy.runtime_account import RuntimeAccountRepository
from tests.fixtures.broker_snapshots import completed_buy_snapshot, open_buy_account

NOW = datetime(2026, 1, 6, 3, tzinfo=UTC)


def _seeded_account():
    account = open_buy_account()
    identity = StrategyIdentity.locked()
    account.code_hash = identity.economic_code_fingerprint
    account.data_hash = "d" * 64
    account.data_hash_as_of = "2026-01-05"
    account.data_hash_symbols = ["sz300308"]
    return account


def test_broker_sync_prepares_without_mutating_production_account_file(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    state_path = tmp_path / "uquant-account.json"
    store = UquantAccountStateStore()
    store.save(_seeded_account(), state_path)
    repository = RuntimeAccountRepository(database=database, path=state_path, clock=lambda: NOW)
    try:
        before = store.hash_file(state_path)

        prepared, receipt = repository.sync_broker_snapshot(completed_buy_snapshot())

        assert receipt.account_before_sha256 == before
        assert receipt.account_after_sha256 == store.hash_state(prepared)
        assert receipt.account_after_sha256 != before
        assert store.hash_file(state_path) == before
        assert database.scalar("SELECT count(*) FROM account_operations") == 0
    finally:
        database.close()
