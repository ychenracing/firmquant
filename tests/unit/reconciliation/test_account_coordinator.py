from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from firmquant.persistence.account_authority import AccountBinding, AccountBindingRepository
from firmquant.persistence.database import Database
from firmquant.persistence.recovery import RecoveryContradiction, UquantAccountStateStore
from firmquant.reconciliation.account_coordinator import (
    AccountReconciliationBlocked,
    AccountReconciliationCoordinator,
)
from firmquant.reconciliation.models import ReconciliationKind
from firmquant.strategy.identity import StrategyIdentity
from firmquant.strategy.runtime_account import RuntimeAccountRepository
from tests.fixtures.broker_snapshots import completed_buy_snapshot, open_buy_account
from tests.fixtures.reconciliation_cases import NOW, healthy_reconciliation_facts


class Reconciler:
    def __init__(
        self,
        *,
        passed: bool = True,
        blockers: tuple[str, ...] = (),
        before_return=None,
    ) -> None:
        self.passed = passed
        self.blockers = blockers
        self.before_return = before_return
        self.calls: list[tuple[object, object]] = []

    def evaluate(self, kind, facts):
        self.calls.append((kind, facts))
        if self.before_return is not None:
            self.before_return()
        return SimpleNamespace(
            reconciliation_id="recon_" + "a" * 64,
            kind=kind,
            passed=self.passed,
            blockers=self.blockers,
        )

    def commit(self, _receipt, *, broker_snapshot_sha256):
        assert len(broker_snapshot_sha256) == 64


def _seeded_account():
    account = open_buy_account()
    identity = StrategyIdentity.locked()
    account.code_hash = identity.economic_code_fingerprint
    account.data_hash = "d" * 64
    account.data_hash_as_of = "2026-01-05"
    account.data_hash_symbols = ["sz300308"]
    return account


def _case(tmp_path: Path, reconciler: Reconciler):
    database = Database.open(tmp_path / "firmquant.sqlite3")
    state_path = tmp_path / "uquant-account.json"
    store = UquantAccountStateStore()
    store.save(_seeded_account(), state_path)
    repository = RuntimeAccountRepository(database=database, path=state_path, clock=lambda: NOW)
    identity = StrategyIdentity.locked()
    snapshot = completed_buy_snapshot()
    binding = AccountBinding.create(
        account_id_hash=snapshot.account.account_id_hash,
        account_type=snapshot.account.account_type,
        broker_snapshot_sha256="a" * 64,
        account_state_sha256=store.hash_file(state_path),
        uquant_commit=identity.uquant_commit,
        uquant_code_fingerprint=identity.economic_code_fingerprint,
        data_hash="d" * 64,
        data_as_of="2026-01-05",
        data_symbols=("sz300308",),
        created_at=NOW,
    )
    AccountBindingRepository(database).bind(binding)
    coordinator = AccountReconciliationCoordinator(
        account_repository=repository,
        reconciler=reconciler,
        cash_tolerance=Decimal("0.01"),
    )
    return database, repository, store, state_path, binding, coordinator


def test_preflight_blocker_never_changes_production_account_file(tmp_path: Path) -> None:
    reconciler = Reconciler()
    database, _repository, store, state_path, binding, coordinator = _case(tmp_path, reconciler)
    try:
        facts = healthy_reconciliation_facts()
        snapshot = facts.broker_snapshot
        changed = replace(
            snapshot,
            account=replace(
                snapshot.account,
                available_cash=type(snapshot.account.available_cash)(
                    snapshot.account.available_cash.value - Decimal("10")
                ),
                total_assets=type(snapshot.account.total_assets)(
                    snapshot.account.total_assets.value - Decimal("10")
                ),
            ),
            raw_payload_sha256="8" * 64,
        )
        before = store.hash_file(state_path)

        with pytest.raises(AccountReconciliationBlocked) as captured:
            coordinator.reconcile(
                kind=ReconciliationKind.STARTUP,
                snapshot=changed,
                operational_ledger=facts.operational_ledger,
                binding=binding,
                final_facts=lambda _account: facts,
            )

        assert "UNEXPLAINED_CASH_CHANGE" in captured.value.blockers
        assert store.hash_file(state_path) == before
        assert database.scalar("SELECT count(*) FROM account_operations") == 0
        assert reconciler.calls == []
    finally:
        database.close()


def test_final_reconciliation_blocker_leaves_prepared_state_in_memory_only(tmp_path: Path) -> None:
    reconciler = Reconciler(passed=False, blockers=("FINAL_BROKER_MISMATCH",))
    database, _repository, store, state_path, binding, coordinator = _case(tmp_path, reconciler)
    try:
        facts = healthy_reconciliation_facts()
        before = store.hash_file(state_path)
        prepared_cash: list[float] = []

        def final_facts(account):
            prepared_cash.append(account.cash)
            return facts

        with pytest.raises(AccountReconciliationBlocked) as captured:
            coordinator.reconcile(
                kind=ReconciliationKind.STARTUP,
                snapshot=facts.broker_snapshot,
                operational_ledger=facts.operational_ledger,
                binding=binding,
                final_facts=final_facts,
            )

        assert captured.value.blockers == ("FINAL_BROKER_MISMATCH",)
        assert prepared_cash == [994.9]
        assert store.hash_file(state_path) == before
        assert database.scalar("SELECT count(*) FROM account_operations") == 0
        assert len(reconciler.calls) == 1
    finally:
        database.close()


def test_passing_reconciliation_commits_once_and_replay_is_noop(tmp_path: Path) -> None:
    reconciler = Reconciler()
    database, _repository, store, state_path, binding, coordinator = _case(tmp_path, reconciler)
    try:
        facts = healthy_reconciliation_facts()
        before = store.hash_file(state_path)

        first = coordinator.reconcile(
            kind=ReconciliationKind.STARTUP,
            snapshot=facts.broker_snapshot,
            operational_ledger=facts.operational_ledger,
            binding=binding,
            final_facts=lambda _account: facts,
        )

        assert first.committed is True
        assert first.account_before_sha256 == before
        assert first.account_after_sha256 != before
        assert store.hash_file(state_path) == first.account_after_sha256
        assert database.scalar("SELECT count(*) FROM account_operations") == 1
        assert database.scalar("SELECT stage FROM account_operations") == "RECEIPT_COMMITTED"

        second = coordinator.reconcile(
            kind=ReconciliationKind.MANUAL,
            snapshot=facts.broker_snapshot,
            operational_ledger=facts.operational_ledger,
            binding=binding,
            final_facts=lambda _account: facts,
        )

        assert second.committed is False
        assert second.account_before_sha256 == first.account_after_sha256
        assert second.account_after_sha256 == first.account_after_sha256
        assert database.scalar("SELECT count(*) FROM account_operations") == 1
        assert len(reconciler.calls) == 2
    finally:
        database.close()


def test_commit_cas_conflict_never_overwrites_concurrent_account_change(tmp_path: Path) -> None:
    reconciler = Reconciler()
    database, repository, store, state_path, binding, coordinator = _case(tmp_path, reconciler)
    try:
        facts = healthy_reconciliation_facts()
        original = store.hash_file(state_path)
        conflict_hash = ""

        def concurrent_change() -> None:
            nonlocal conflict_hash
            account = repository.load()
            account.cash -= 1.0
            store.save(account, state_path)
            conflict_hash = store.hash_file(state_path)

        reconciler.before_return = concurrent_change

        with pytest.raises(RecoveryContradiction):
            coordinator.reconcile(
                kind=ReconciliationKind.STARTUP,
                snapshot=facts.broker_snapshot,
                operational_ledger=facts.operational_ledger,
                binding=binding,
                final_facts=lambda _account: facts,
            )

        assert conflict_hash
        assert conflict_hash != original
        assert store.hash_file(state_path) == conflict_hash
        assert database.scalar("SELECT count(*) FROM account_operations") == 0
    finally:
        database.close()
