from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from firmquant.domain.values import Money
from firmquant.persistence.database import Database
from firmquant.persistence.recovery import AccountOperation, RecoveryService
from firmquant.reconciliation.models import ReconciliationKind
from firmquant.reconciliation.service import (
    ReconciliationService,
    reconciliation_finalization_payload,
)
from tests.fixtures.reconciliation_cases import NOW, healthy_reconciliation_facts
from tests.fixtures.recovery_cases import JsonAccountStateStore, write_account


def test_file_committed_broker_sync_recovers_reconciliation_finalization_once(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    store = JsonAccountStateStore()
    account_path = tmp_path / "account.json"
    before = {"cash": "1000"}
    after = {"cash": "900"}
    write_account(account_path, before, store)
    facts = healthy_reconciliation_facts()
    reconciler = ReconciliationService(
        database=database,
        cash_tolerance=Money(Decimal("0.01")),
        clock=lambda: NOW,
    )
    receipt = reconciler.evaluate(ReconciliationKind.STARTUP, facts)
    finalization = reconciliation_finalization_payload(
        receipt,
        broker_snapshot_sha256=facts.broker_snapshot.raw_payload_sha256,
    )
    operation = AccountOperation.begin(
        database=database,
        store=store,
        account_path=account_path,
        prepared_account=after,
        expected_before_sha256=store.hash_state(before),
        operation_kind="BROKER_SYNC",
        evidence_sha256=facts.broker_snapshot.raw_payload_sha256,
        finalization_payload=finalization,
        now=NOW,
        operation_id="acctop_" + "f" * 64,
    )
    operation.commit_file(now=NOW)

    first = RecoveryService(
        database=database,
        account_store=store,
        account_path=account_path,
        gateway=None,
        clock=lambda: NOW,
    ).recover()

    row = database.query_one(
        "SELECT stage, actual_account_after_sha256 FROM account_operations WHERE operation_id = ?",
        (operation.operation_id,),
    )
    assert row is not None
    assert row["stage"] == "RECEIPT_COMMITTED"
    assert row["actual_account_after_sha256"] == operation.expected_account_after_sha256
    assert "ACCOUNT_FINALIZATION_REQUIRED" not in first.blockers
    assert database.scalar("SELECT count(*) FROM reconciliation_runs") == 1
    assert database.scalar(
        "SELECT count(*) FROM audit_events WHERE category = 'reconciliation.receipt'"
    ) == 1

    second = RecoveryService(
        database=database,
        account_store=store,
        account_path=account_path,
        gateway=None,
        clock=lambda: NOW,
    ).recover()
    assert second.account_receipts == ()
    assert database.scalar("SELECT count(*) FROM reconciliation_runs") == 1
    database.close()
