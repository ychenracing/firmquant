from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

import firmquant.application.production_services as ps
from firmquant.application.production_services import ProductionServicesUnavailable
from firmquant.persistence.account_authority import AccountBinding, AccountBindingRepository
from firmquant.reconciliation.models import ReconciliationFacts, ReconciliationKind
from firmquant.strategy.identity import StrategyIdentity
from tests.fixtures.session_cases import NOW, execution_snapshot
from tests.unit.application.test_production_services_acceptance import (
    PassingReconciler,
    hook_case,
)


def _bind_account(database) -> AccountBinding:
    snapshot = execution_snapshot().broker_snapshot
    identity = StrategyIdentity.locked()
    binding = AccountBinding.create(
        account_id_hash=snapshot.account.account_id_hash,
        account_type=snapshot.account.account_type,
        broker_snapshot_sha256="a" * 64,
        account_state_sha256="c" * 64,
        uquant_commit=identity.uquant_commit,
        uquant_code_fingerprint=identity.economic_code_fingerprint,
        data_hash="d" * 64,
        data_as_of="2026-08-24",
        data_symbols=("sz300308",),
        created_at=NOW,
    )
    return AccountBindingRepository(database).bind(binding)


def _identity_green(hooks, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ps, "_data_identity_matches", lambda *_args: True)
    monkeypatch.setattr(
        ps,
        "configuration_sha256",
        lambda _path: hooks._identity.config_sha256,
    )


def test_reconcile_requires_persistent_binding_before_any_account_sync(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with hook_case(tmp_path) as (hooks, _writer, _broker, accounts):
        hooks._reconciler = PassingReconciler()
        _identity_green(hooks, monkeypatch)

        def forbidden_sync(_snapshot):
            raise AssertionError("legacy account sync must not run before binding authority")

        accounts.sync_broker_snapshot = forbidden_sync

        with pytest.raises(ProductionServicesUnavailable, match="ACCOUNT_BINDING_REQUIRED"):
            hooks._reconcile(ReconciliationKind.STARTUP)


def test_reconcile_routes_bound_account_through_gated_coordinator(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class RecordingCoordinator:
        def __init__(self, *, account_repository, reconciler, cash_tolerance: Decimal) -> None:
            calls.append(
                {
                    "account_repository": account_repository,
                    "reconciler": reconciler,
                    "cash_tolerance": cash_tolerance,
                }
            )
            self._accounts = account_repository
            self._reconciler = reconciler

        def reconcile(self, *, kind, snapshot, operational_ledger, binding, final_facts):
            candidate = self._accounts.load()
            facts = final_facts(candidate)
            assert isinstance(facts, ReconciliationFacts)
            assert facts.broker_snapshot == snapshot
            assert facts.operational_ledger == operational_ledger
            assert operational_ledger.expected_account_id_hash == binding.account_id_hash
            assert operational_ledger.expected_account_type is binding.account_type
            receipt = self._reconciler.run(kind, facts)
            return SimpleNamespace(receipt=receipt, account=candidate)

    monkeypatch.setattr(ps, "AccountReconciliationCoordinator", RecordingCoordinator, raising=False)

    with hook_case(tmp_path) as (hooks, writer, _broker, accounts):
        binding = _bind_account(writer.database)
        reconciler = PassingReconciler()
        hooks._reconciler = reconciler
        _identity_green(hooks, monkeypatch)

        def forbidden_sync(_snapshot):
            raise AssertionError("legacy sync_broker_snapshot path must not be used")

        accounts.sync_broker_snapshot = forbidden_sync

        receipt, snapshot, account = hooks._reconcile(ReconciliationKind.STARTUP)

        assert receipt.passed is True
        assert account is accounts.account
        assert snapshot.account.account_id_hash == binding.account_id_hash
        assert len(calls) == 1
        assert calls[0]["account_repository"] is accounts
        assert calls[0]["reconciler"] is reconciler
        assert calls[0]["cash_tolerance"] == Decimal("0.01")
        assert len(reconciler.facts) == 1
