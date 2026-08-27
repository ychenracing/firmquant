from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from firmquant.domain.broker_facts import AccountType
from firmquant.domain.values import Money
from firmquant.persistence.account_authority import AccountBinding
from firmquant.persistence.database import Database
from firmquant.persistence.recovery import RecoveryContradiction, UquantAccountStateStore
from firmquant.reconciliation.account_coordinator import AccountReconciliationCoordinator
from firmquant.reconciliation.models import ReconciliationKind
from firmquant.reconciliation.service import ReconciliationService
from firmquant.strategy.account_prepare import prepare_account_sync
from firmquant.strategy.identity import StrategyIdentity
from tests.fixtures.broker_snapshots import completed_buy_snapshot, open_buy_account
from tests.fixtures.reconciliation_cases import healthy_reconciliation_facts

NOW = datetime(2026, 1, 6, 8, 5, tzinfo=UTC)


def _service(database: Database) -> ReconciliationService:
    return ReconciliationService(
        database=database,
        cash_tolerance=Money(Decimal("0.01")),
        clock=lambda: NOW,
    )


def _binding() -> AccountBinding:
    snapshot = completed_buy_snapshot()
    return AccountBinding.create(
        account_id_hash=snapshot.account.account_id_hash,
        account_type=AccountType.CASH,
        broker_snapshot_sha256="a" * 64,
        account_state_sha256="b" * 64,
        uquant_commit="1" * 40,
        uquant_code_fingerprint="c" * 64,
        data_hash="d" * 64,
        data_as_of="2026-01-05",
        data_symbols=("sz300308",),
        created_at=NOW,
    )


def _seeded_account():
    account = open_buy_account()
    identity = StrategyIdentity.locked()
    account.code_hash = identity.economic_code_fingerprint
    account.data_hash = "d" * 64
    account.data_hash_as_of = "2026-01-05"
    account.data_hash_symbols = ["sz300308"]
    return account


def test_reconciliation_evaluation_is_pure_until_explicit_commit(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    facts = healthy_reconciliation_facts()
    reconciler = _service(database)
    try:
        receipt = reconciler.evaluate(ReconciliationKind.STARTUP, facts)

        assert receipt.passed is True
        assert database.scalar("SELECT count(*) FROM reconciliation_runs") == 0
        assert database.scalar("SELECT count(*) FROM audit_events") == 0

        reconciler.commit(
            receipt,
            broker_snapshot_sha256=facts.broker_snapshot.raw_payload_sha256,
        )
        reconciler.commit(
            receipt,
            broker_snapshot_sha256=facts.broker_snapshot.raw_payload_sha256,
        )

        assert database.scalar("SELECT count(*) FROM reconciliation_runs") == 1
        assert database.scalar("SELECT count(*) FROM audit_events") == 1
    finally:
        database.close()


class _CommitRejectedAccountRepository:
    def __init__(self) -> None:
        self._account = _seeded_account()
        self._store = UquantAccountStateStore()

    @property
    def store(self) -> UquantAccountStateStore:
        return self._store

    def load(self):
        return self._account

    def prepare_broker_snapshot(self, snapshot):
        return prepare_account_sync(self._account, snapshot)

    def commit_broker_snapshot(
        self,
        _prepared,
        *,
        finalize=None,
        finalization_payload=None,
    ):
        del finalize, finalization_payload
        raise RecoveryContradiction("injected account commit failure")


def test_failed_account_commit_cannot_publish_passed_reconciliation_receipt(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    facts = healthy_reconciliation_facts()
    coordinator = AccountReconciliationCoordinator(
        account_repository=_CommitRejectedAccountRepository(),
        reconciler=_service(database),
        cash_tolerance=Decimal("0.01"),
    )
    try:
        with pytest.raises(RecoveryContradiction, match="injected account commit failure"):
            coordinator.reconcile(
                kind=ReconciliationKind.STARTUP,
                snapshot=facts.broker_snapshot,
                operational_ledger=facts.operational_ledger,
                binding=_binding(),
                final_facts=lambda _account: facts,
            )

        assert database.scalar("SELECT count(*) FROM reconciliation_runs") == 0
        assert (
            database.scalar("SELECT count(*) FROM audit_events WHERE category = 'reconciliation.receipt'")
            == 0
        )
    finally:
        database.close()
