from __future__ import annotations

from pathlib import Path

from firmquant.persistence.database import Database
from firmquant.persistence.recovery import (
    AccountOperation,
    AccountRecoveryClassification,
    RecoveryService,
)
from tests.fixtures.recovery_cases import NOW, JsonAccountStateStore, write_account


def _operation(tmp_path: Path, database: Database) -> tuple[AccountOperation, JsonAccountStateStore, Path]:
    store = JsonAccountStateStore()
    account_path = tmp_path / "account.json"
    before = {"cash": "1000"}
    after = {"cash": "900"}
    write_account(account_path, before, store)
    operation = AccountOperation.begin(
        database=database,
        store=store,
        account_path=account_path,
        prepared_account=after,
        expected_before_sha256=store.hash_state(before),
        operation_kind="BROKER_SYNC",
        evidence_sha256="a" * 64,
        now=NOW,
        operation_id="acctop_" + "a" * 64,
    )
    return operation, store, account_path


def _recover(database: Database, store: JsonAccountStateStore, account_path: Path):
    return RecoveryService(
        database=database,
        account_store=store,
        account_path=account_path,
        gateway=None,
        clock=lambda: NOW,
    ).recover()


def test_broker_sync_recovery_preserves_prepared_stage_for_retry(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        operation, store, account_path = _operation(tmp_path, database)

        report = _recover(database, store, account_path)

        assert report.account_receipts[0].classification is AccountRecoveryClassification.NOT_APPLIED
        assert "ACCOUNT_COMMIT_RETRY_REQUIRED" in report.blockers
        row = database.query_one(
            "SELECT stage, actual_account_after_sha256 FROM account_operations WHERE operation_id = ?",
            (operation.operation_id,),
        )
        assert row is not None
        assert row["stage"] == "PREPARED"
        assert row["actual_account_after_sha256"] is None
    finally:
        database.close()


def test_broker_sync_recovery_preserves_file_committed_stage_for_finalization(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        operation, store, account_path = _operation(tmp_path, database)
        operation.commit_file(now=NOW)

        report = _recover(database, store, account_path)

        assert (
            report.account_receipts[0].classification
            is AccountRecoveryClassification.FILE_APPLIED_RECEIPT_MISSING
        )
        assert "ACCOUNT_FINALIZATION_REQUIRED" in report.blockers
        row = database.query_one(
            "SELECT stage, actual_account_after_sha256 FROM account_operations WHERE operation_id = ?",
            (operation.operation_id,),
        )
        assert row is not None
        assert row["stage"] == "FILE_COMMITTED"
        assert row["actual_account_after_sha256"] == operation.expected_account_after_sha256
    finally:
        database.close()
