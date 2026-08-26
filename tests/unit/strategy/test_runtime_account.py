from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from firmquant.persistence.database import Database
from firmquant.persistence.recovery import UquantAccountStateStore
from firmquant.strategy.identity import StrategyIdentity
from firmquant.strategy.runtime_account import RuntimeAccountRepository
from tests.fixtures.broker_snapshots import completed_buy_snapshot, open_buy_account

NOW = datetime(2026, 1, 6, 3, tzinfo=UTC)


def seeded_account():
    account = open_buy_account()
    identity = StrategyIdentity.locked()
    account.code_hash = identity.economic_code_fingerprint
    account.data_hash = "d" * 64
    account.data_hash_as_of = "2026-01-05"
    account.data_hash_symbols = ["sz300308"]
    return account


def test_broker_sync_is_atomically_persisted_with_account_operation_receipt(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    state_path = tmp_path / "uquant-account.json"
    store = UquantAccountStateStore(state_path)
    store.commit_state(seeded_account())
    repository = RuntimeAccountRepository(
        database=database,
        path=state_path,
        clock=lambda: NOW,
    )
    try:
        account, receipt = repository.sync_broker_snapshot(completed_buy_snapshot())

        assert receipt.fills_imported == 1
        assert account.cash == 994.9
        reloaded = repository.load()
        assert reloaded.cash == 994.9
        row = database.query_one(
            "SELECT operation_kind, stage, actual_account_after_sha256 FROM account_operations"
        )
        assert row is not None
        assert row["operation_kind"] == "BROKER_SYNC"
        assert row["stage"] == "RECEIPT_COMMITTED"
        assert row["actual_account_after_sha256"] == receipt.account_after_sha256
    finally:
        database.close()


def test_persist_prepared_state_rejects_wrong_before_hash_without_overwrite(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    state_path = tmp_path / "uquant-account.json"
    store = UquantAccountStateStore(state_path)
    account = seeded_account()
    store.commit_state(account)
    repository = RuntimeAccountRepository(database=database, path=state_path, clock=lambda: NOW)
    try:
        before = store.read_state_hash()
        prepared = repository.load()
        prepared.cash -= 1
        try:
            repository.persist_prepared(
                prepared,
                expected_before_sha256="0" * 64,
                operation_kind="DECISION_COMMIT",
                evidence_sha256="e" * 64,
            )
        except Exception:
            pass
        else:
            raise AssertionError("wrong before hash must fail")
        assert store.read_state_hash() == before
    finally:
        database.close()
