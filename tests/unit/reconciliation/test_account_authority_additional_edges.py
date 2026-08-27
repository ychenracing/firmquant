from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from firmquant.domain.broker_facts import AccountType, Side
from firmquant.domain.errors import DomainTypeError, DomainValidationError
from firmquant.domain.values import Money, Symbol
from firmquant.persistence.account_authority import (
    AccountBinding,
    AccountBindingRepository,
    AdjustmentCoverage,
    ReviewedAccountAdjustment,
    ReviewedAccountAdjustmentRepository,
    ensure_account_authority_schema,
)
from firmquant.persistence.database import Database
from firmquant.persistence.recovery import RecoveryContradiction, UquantAccountStateStore
from firmquant.persistence.repositories import PersistenceConflict, canonical_json
from firmquant.reconciliation import account_preflight as preflight_module
from firmquant.reconciliation.account_coordinator import (
    AccountReconciliationBlocked,
    AccountReconciliationCoordinator,
    AccountReconciliationResult,
)
from firmquant.reconciliation.account_preflight import (
    AccountPreflightResult,
    account_difference_sha256,
    evaluate_account_preflight,
)
from firmquant.reconciliation.models import ReconciliationKind
from firmquant.strategy import runtime_account as runtime_account_module
from firmquant.strategy.account_sync import sync_account
from firmquant.strategy.identity import StrategyIdentity
from firmquant.strategy.runtime_account import RuntimeAccountRepository
from tests.fixtures.broker_snapshots import completed_buy_snapshot, open_buy_account
from tests.fixtures.reconciliation_cases import NOW, healthy_reconciliation_facts


def _binding_kwargs() -> dict[str, object]:
    snapshot = completed_buy_snapshot()
    return {
        "account_id_hash": snapshot.account.account_id_hash,
        "account_type": AccountType.CASH,
        "broker_snapshot_sha256": "a" * 64,
        "account_state_sha256": "b" * 64,
        "uquant_commit": "1" * 40,
        "uquant_code_fingerprint": "c" * 64,
        "data_hash": "d" * 64,
        "data_as_of": "2026-01-05",
        "data_symbols": ("sz300308",),
        "created_at": NOW,
    }


def _binding(**overrides: object) -> AccountBinding:
    values = _binding_kwargs()
    values.update(overrides)
    return AccountBinding.create(**values)  # type: ignore[arg-type]


def _adjustment_kwargs() -> dict[str, object]:
    return {
        "account_id_hash": "a" * 64,
        "symbol": Symbol.parse("sz300308"),
        "session": date(2026, 1, 6),
        "adjustment_type": "CORPORATE_ACTION",
        "coverage": AdjustmentCoverage.POSITION_TOTAL_SHARES,
        "broker_snapshot_sha256": "b" * 64,
        "difference_sha256": "c" * 64,
        "audit_summary_sha256": "d" * 64,
        "created_at": NOW,
    }


def _adjustment(**overrides: object) -> ReviewedAccountAdjustment:
    values = _adjustment_kwargs()
    values.update(overrides)
    return ReviewedAccountAdjustment.create(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        {"account_id_hash": "x" * 64},
        {"account_type": AccountType.MARGIN},
        {"broker_snapshot_sha256": "x" * 64},
        {"account_state_sha256": "x" * 64},
        {"uquant_commit": "x" * 40},
        {"uquant_code_fingerprint": "x" * 64},
        {"data_hash": "x" * 64},
        {"data_as_of": " bad"},
        {"data_symbols": ()},
        {"data_symbols": ("sz300308", "sz300308")},
        {"data_symbols": ("bad",)},
        {"created_at": datetime(2026, 1, 6, 3)},
    ],
)
def test_account_binding_rejects_noncanonical_contract(overrides: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        _binding(**overrides)


def test_account_authority_schema_and_repository_reject_wrong_database() -> None:
    with pytest.raises(TypeError):
        ensure_account_authority_schema(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        AccountBindingRepository(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ReviewedAccountAdjustmentRepository(object())  # type: ignore[arg-type]


def test_account_binding_repository_rejects_invalid_rows_and_binding_collisions(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    repository = AccountBindingRepository(database)
    first = _binding()
    try:
        with pytest.raises(PersistenceConflict):
            repository._from_row(object())
        with pytest.raises(PersistenceConflict):
            repository._from_row({"data_symbols_json": "not-json"})

        row = {
            "binding_id": "acctbind_" + "0" * 64,
            "account_id_hash": first.account_id_hash,
            "account_type": first.account_type.value,
            "broker_snapshot_sha256": first.broker_snapshot_sha256,
            "account_state_sha256": first.account_state_sha256,
            "uquant_commit": first.uquant_commit,
            "uquant_code_fingerprint": first.uquant_code_fingerprint,
            "data_hash": first.data_hash,
            "data_as_of": first.data_as_of,
            "data_symbols_json": canonical_json(first.data_symbols),
            "created_at": first.created_at.isoformat(),
            "payload_json": first.payload_json,
            "payload_sha256": first.payload_sha256,
        }
        with pytest.raises(PersistenceConflict):
            repository._from_row(row)

        with pytest.raises(TypeError):
            repository.bind(object())  # type: ignore[arg-type]
        assert repository.bind(first) == first
        second = _binding(broker_snapshot_sha256="e" * 64)
        with pytest.raises(PersistenceConflict):
            repository.bind(second)
    finally:
        database.close()


@pytest.mark.parametrize(
    "overrides",
    [
        {"account_id_hash": "x" * 64},
        {"symbol": "sz300308"},
        {"session": NOW},
        {"adjustment_type": " bad"},
        {"coverage": "POSITION_TOTAL_SHARES"},
        {"broker_snapshot_sha256": "x" * 64},
        {"difference_sha256": "x" * 64},
        {"audit_summary_sha256": "x" * 64},
        {"created_at": datetime(2026, 1, 6, 3)},
    ],
)
def test_reviewed_adjustment_rejects_noncanonical_contract(overrides: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        _adjustment(**overrides)


def test_reviewed_adjustment_repository_detects_idempotency_collision_and_corruption(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    repository = ReviewedAccountAdjustmentRepository(database)
    adjustment = _adjustment()
    try:
        with pytest.raises(TypeError):
            repository.append(object())  # type: ignore[arg-type]
        assert repository.append(adjustment) is True
        assert repository.append(adjustment) is False
        collision = replace(adjustment, payload_json="{}")
        with pytest.raises(PersistenceConflict):
            repository.append(collision)

        with pytest.raises(TypeError):
            repository.matching_ids(
                account_id_hash="a" * 64,
                symbol="sz300308",  # type: ignore[arg-type]
                session=date(2026, 1, 6),
                coverage=AdjustmentCoverage.POSITION_TOTAL_SHARES,
                broker_snapshot_sha256="b" * 64,
                difference_sha256="c" * 64,
            )
        with pytest.raises(TypeError):
            repository.matching_ids(
                account_id_hash="a" * 64,
                symbol=None,
                session=NOW,  # type: ignore[arg-type]
                coverage=AdjustmentCoverage.POSITION_TOTAL_SHARES,
                broker_snapshot_sha256="b" * 64,
                difference_sha256="c" * 64,
            )
        with pytest.raises(TypeError):
            repository.matching_ids(
                account_id_hash="a" * 64,
                symbol=None,
                session=date(2026, 1, 6),
                coverage="POSITION_TOTAL_SHARES",  # type: ignore[arg-type]
                broker_snapshot_sha256="b" * 64,
                difference_sha256="c" * 64,
            )
        with pytest.raises(ValueError):
            repository.matching_ids(
                account_id_hash="x" * 64,
                symbol=None,
                session=date(2026, 1, 6),
                coverage=AdjustmentCoverage.POSITION_TOTAL_SHARES,
                broker_snapshot_sha256="b" * 64,
                difference_sha256="c" * 64,
            )

        assert repository.matching_ids(
            account_id_hash=adjustment.account_id_hash,
            symbol=None,
            session=adjustment.session,
            coverage=adjustment.coverage,
            broker_snapshot_sha256=adjustment.broker_snapshot_sha256,
            difference_sha256=adjustment.difference_sha256,
        ) == (adjustment.adjustment_id,)

        corrupt_database = Database.open(tmp_path / "corrupt.sqlite3")
        corrupt_repository = ReviewedAccountAdjustmentRepository(corrupt_database)
        try:
            with corrupt_database.transaction():
                corrupt_database.write(
                    """
                    INSERT INTO reviewed_account_adjustments(
                        adjustment_id, account_id_hash, symbol, session_date, adjustment_type,
                        coverage_kind, broker_snapshot_sha256, difference_sha256,
                        audit_summary_sha256, payload_json, payload_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "acctadj_" + "f" * 64,
                        "a" * 64,
                        "sz300308",
                        "2026-01-06",
                        "CORPORATE_ACTION",
                        AdjustmentCoverage.POSITION_TOTAL_SHARES.value,
                        "b" * 64,
                        "c" * 64,
                        "d" * 64,
                        "{}",
                        "e" * 64,
                        NOW.isoformat(),
                    ),
                )
            with pytest.raises(PersistenceConflict):
                corrupt_repository.matching_ids(
                    account_id_hash="a" * 64,
                    symbol=Symbol.parse("sz300308"),
                    session=date(2026, 1, 6),
                    coverage=AdjustmentCoverage.POSITION_TOTAL_SHARES,
                    broker_snapshot_sha256="b" * 64,
                    difference_sha256="c" * 64,
                )
        finally:
            corrupt_database.close()
    finally:
        database.close()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"passed": 1, "blockers": (), "explained_fill_ids": ()},
        {"passed": False, "blockers": ("B", "A"), "explained_fill_ids": ()},
        {"passed": False, "blockers": ("A",), "explained_fill_ids": ("f", "f")},
        {
            "passed": False,
            "blockers": ("A",),
            "explained_fill_ids": (),
            "reviewed_adjustment_ids": ("r", "r"),
        },
        {"passed": True, "blockers": ("A",), "explained_fill_ids": ()},
    ],
)
def test_account_preflight_result_rejects_inconsistent_identity(kwargs: dict[str, object]) -> None:
    with pytest.raises((DomainTypeError, DomainValidationError)):
        AccountPreflightResult(**kwargs)  # type: ignore[arg-type]


def _difference_kwargs() -> dict[str, object]:
    snapshot = completed_buy_snapshot()
    return {
        "account_id_hash": snapshot.account.account_id_hash,
        "symbol": None,
        "session": snapshot.session_date,
        "coverage": AdjustmentCoverage.AVAILABLE_CASH,
        "broker_snapshot_sha256": snapshot.raw_payload_sha256,
        "expected": Decimal("1000"),
        "observed": Decimal("999"),
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"account_id_hash": "x" * 64},
        {"broker_snapshot_sha256": "x" * 64},
        {"session": NOW},
        {"coverage": "AVAILABLE_CASH"},
        {"symbol": "sz300308"},
        {"coverage": AdjustmentCoverage.POSITION_TOTAL_SHARES, "symbol": None},
        {"coverage": AdjustmentCoverage.AVAILABLE_CASH, "symbol": Symbol.parse("sz300308")},
        {"expected": True},
        {"expected": Decimal("NaN")},
        {"observed": -1},
    ],
)
def test_account_difference_rejects_ambiguous_or_unsafe_values(overrides: dict[str, object]) -> None:
    values = _difference_kwargs()
    values.update(overrides)
    with pytest.raises((DomainTypeError, DomainValidationError)):
        account_difference_sha256(**values)  # type: ignore[arg-type]


def test_preflight_private_account_contract_helpers_reject_corrupt_uquant_state() -> None:
    with pytest.raises(DomainTypeError):
        preflight_module._account_cash(SimpleNamespace(cash=True))
    with pytest.raises(DomainValidationError):
        preflight_module._account_cash(SimpleNamespace(cash=float("nan")))
    with pytest.raises(DomainValidationError):
        preflight_module._account_cash(SimpleNamespace(cash=-1.0))

    invalid_symbol = SimpleNamespace(
        positions={"": SimpleNamespace(shares=1, sellable_shares=lambda _session: 1)}
    )
    with pytest.raises(DomainValidationError):
        preflight_module._current_positions(invalid_symbol, session="2026-01-06")

    invalid_quantities = SimpleNamespace(
        positions={"sz300308": SimpleNamespace(shares=0, sellable_shares=lambda _session: 0)}
    )
    with pytest.raises(DomainValidationError):
        preflight_module._current_positions(invalid_quantities, session="2026-01-06")

    duplicate_orders = SimpleNamespace(
        order_ledger=[
            SimpleNamespace(order_id="O1"),
            SimpleNamespace(order_id="O1"),
        ]
    )
    with pytest.raises(DomainValidationError):
        preflight_module._known_account_orders(duplicate_orders)

    fills = SimpleNamespace(
        fills=[
            SimpleNamespace(fill_id=""),
            SimpleNamespace(fill_id="F1"),
            SimpleNamespace(fill_id="F1"),
        ]
    )
    with pytest.raises(DomainValidationError):
        preflight_module._known_account_fill_ids(fills)


def test_evaluate_preflight_rejects_wrong_typed_ports() -> None:
    facts = healthy_reconciliation_facts()
    account = open_buy_account()
    binding = _binding()
    tolerance = Money(Decimal("0.01"))

    with pytest.raises(DomainTypeError):
        evaluate_account_preflight(
            snapshot=object(),  # type: ignore[arg-type]
            account=account,
            operational_ledger=facts.operational_ledger,
            binding=binding,
            cash_tolerance=tolerance,
        )
    with pytest.raises(DomainTypeError):
        evaluate_account_preflight(
            snapshot=facts.broker_snapshot,
            account=account,
            operational_ledger=object(),  # type: ignore[arg-type]
            binding=binding,
            cash_tolerance=tolerance,
        )
    with pytest.raises(DomainTypeError):
        evaluate_account_preflight(
            snapshot=facts.broker_snapshot,
            account=account,
            operational_ledger=facts.operational_ledger,
            binding=object(),  # type: ignore[arg-type]
            cash_tolerance=tolerance,
        )
    with pytest.raises(DomainTypeError):
        evaluate_account_preflight(
            snapshot=facts.broker_snapshot,
            account=account,
            operational_ledger=facts.operational_ledger,
            binding=binding,
            cash_tolerance=Decimal("0.01"),  # type: ignore[arg-type]
        )
    with pytest.raises(DomainTypeError):
        evaluate_account_preflight(
            snapshot=facts.broker_snapshot,
            account=account,
            operational_ledger=facts.operational_ledger,
            binding=binding,
            cash_tolerance=tolerance,
            reviewed_adjustments=object(),  # type: ignore[arg-type]
        )


def test_preflight_reports_identity_order_fill_and_intent_mismatches() -> None:
    facts = healthy_reconciliation_facts()
    tolerance = Money(Decimal("0.01"))

    wrong_binding = replace(_binding(), account_id_hash="f" * 64)
    wrong_type_ledger = replace(facts.operational_ledger, expected_account_type=AccountType.MARGIN)
    identity = evaluate_account_preflight(
        snapshot=facts.broker_snapshot,
        account=open_buy_account(),
        operational_ledger=wrong_type_ledger,
        binding=wrong_binding,
        cash_tolerance=tolerance,
    )
    assert "ACCOUNT_IDENTITY_CHANGED" in identity.blockers
    assert "ACCOUNT_TYPE_CHANGED" in identity.blockers

    local_order = facts.operational_ledger.orders[0]
    mismatched_order = replace(local_order, uquant_order_id="OTHER", side=Side.SELL)
    order_result = evaluate_account_preflight(
        snapshot=facts.broker_snapshot,
        account=open_buy_account(),
        operational_ledger=replace(facts.operational_ledger, orders=(mismatched_order,)),
        binding=_binding(),
        cash_tolerance=tolerance,
    )
    assert "BROKER_ORDER_IDENTITY_MISMATCH" in order_result.blockers
    assert "BROKER_FILL_IDENTITY_MISMATCH" in order_result.blockers

    unmapped = evaluate_account_preflight(
        snapshot=facts.broker_snapshot,
        account=open_buy_account(),
        operational_ledger=replace(facts.operational_ledger, known_broker_fill_ids=frozenset()),
        binding=_binding(),
        cash_tolerance=tolerance,
    )
    assert "UNMAPPED_BROKER_FILL" in unmapped.blockers

    account = open_buy_account()
    account.order_ledger[0].order_id = "DIFFERENT"
    missing_intent = evaluate_account_preflight(
        snapshot=facts.broker_snapshot,
        account=account,
        operational_ledger=facts.operational_ledger,
        binding=_binding(),
        cash_tolerance=tolerance,
    )
    assert "BROKER_FILL_WITHOUT_UQUANT_INTENT" in missing_intent.blockers

    imported = open_buy_account()
    sync_account(imported, facts.broker_snapshot)
    imported_result = evaluate_account_preflight(
        snapshot=facts.broker_snapshot,
        account=imported,
        operational_ledger=facts.operational_ledger,
        binding=_binding(),
        cash_tolerance=tolerance,
    )
    assert imported_result.passed is True
    assert imported_result.explained_fill_ids == ("broker-fill-1",)


@pytest.mark.parametrize(
    "blockers",
    [(), ("B", "A"), ("A", "A"), (" bad ",)],
)
def test_account_reconciliation_blocked_requires_canonical_blockers(blockers: tuple[str, ...]) -> None:
    with pytest.raises(DomainValidationError):
        AccountReconciliationBlocked(blockers)


def test_account_reconciliation_result_rejects_invalid_commit_identity() -> None:
    preflight = AccountPreflightResult(passed=True, blockers=(), explained_fill_ids=())
    receipt = SimpleNamespace()
    account = SimpleNamespace()

    with pytest.raises(DomainTypeError):
        AccountReconciliationResult(
            receipt=receipt,  # type: ignore[arg-type]
            account=account,  # type: ignore[arg-type]
            preflight=preflight,
            account_before_sha256="a" * 64,
            account_after_sha256="b" * 64,
            committed=1,  # type: ignore[arg-type]
        )
    with pytest.raises(DomainValidationError):
        AccountReconciliationResult(
            receipt=receipt,  # type: ignore[arg-type]
            account=account,  # type: ignore[arg-type]
            preflight=preflight,
            account_before_sha256="x" * 64,
            account_after_sha256="b" * 64,
            committed=False,
        )
    with pytest.raises(DomainValidationError):
        AccountReconciliationResult(
            receipt=receipt,  # type: ignore[arg-type]
            account=account,  # type: ignore[arg-type]
            preflight=preflight,
            account_before_sha256="a" * 64,
            account_after_sha256="a" * 64,
            committed=True,
        )


class _FakeStore:
    def __init__(self, digest: str) -> None:
        self.digest = digest

    def hash_state(self, _state: object) -> str:
        return self.digest


class _FakeAccounts:
    def __init__(
        self,
        *,
        before: str = "a" * 64,
        prepared_before: str | None = None,
        after: str = "b" * 64,
        commit_result: str | None = None,
    ) -> None:
        self.current = open_buy_account()
        self.store = _FakeStore(before)
        self.prepared = SimpleNamespace(
            account_before_sha256=prepared_before or before,
            account_after_sha256=after,
            prepared_account=open_buy_account(),
        )
        self.commit_result = after if commit_result is None else commit_result

    def load(self):
        return self.current

    def prepare_broker_snapshot(self, _snapshot):
        return self.prepared

    def commit_broker_snapshot(self, _prepared):
        return self.commit_result


class _PassingReconciler:
    def run(self, _kind, _facts):
        return SimpleNamespace(passed=True, blockers=())


def _fake_coordinator(accounts: object | None = None) -> AccountReconciliationCoordinator:
    return AccountReconciliationCoordinator(
        account_repository=accounts or _FakeAccounts(),  # type: ignore[arg-type]
        reconciler=_PassingReconciler(),  # type: ignore[arg-type]
        cash_tolerance=Decimal("0.01"),
    )


def test_account_reconciliation_coordinator_rejects_invalid_constructor_and_inputs() -> None:
    with pytest.raises(DomainTypeError):
        AccountReconciliationCoordinator(
            account_repository=object(),  # type: ignore[arg-type]
            reconciler=object(),  # type: ignore[arg-type]
            cash_tolerance=1,  # type: ignore[arg-type]
        )
    for tolerance in (Decimal("NaN"), Decimal("-0.01")):
        with pytest.raises(DomainValidationError):
            AccountReconciliationCoordinator(
                account_repository=object(),  # type: ignore[arg-type]
                reconciler=object(),  # type: ignore[arg-type]
                cash_tolerance=tolerance,
            )
    with pytest.raises(DomainTypeError):
        AccountReconciliationCoordinator(
            account_repository=object(),  # type: ignore[arg-type]
            reconciler=object(),  # type: ignore[arg-type]
            cash_tolerance=Decimal("0"),
            reviewed_adjustments=object(),  # type: ignore[arg-type]
        )

    facts = healthy_reconciliation_facts()
    coordinator = _fake_coordinator()
    binding = _binding()
    valid = {
        "kind": ReconciliationKind.STARTUP,
        "snapshot": facts.broker_snapshot,
        "operational_ledger": facts.operational_ledger,
        "binding": binding,
        "final_facts": lambda _account: facts,
    }
    invalid_values = [
        {"kind": "STARTUP"},
        {"snapshot": object()},
        {"operational_ledger": object()},
        {"binding": object()},
        {"final_facts": None},
    ]
    for override in invalid_values:
        kwargs = dict(valid)
        kwargs.update(override)
        with pytest.raises(DomainTypeError):
            coordinator.reconcile(**kwargs)  # type: ignore[arg-type]


def test_account_reconciliation_coordinator_detects_all_post_preflight_drift() -> None:
    facts = healthy_reconciliation_facts()
    binding = _binding()

    with pytest.raises(RecoveryContradiction):
        _fake_coordinator(_FakeAccounts(prepared_before="c" * 64)).reconcile(
            kind=ReconciliationKind.STARTUP,
            snapshot=facts.broker_snapshot,
            operational_ledger=facts.operational_ledger,
            binding=binding,
            final_facts=lambda _account: facts,
        )

    with pytest.raises(DomainTypeError):
        _fake_coordinator().reconcile(
            kind=ReconciliationKind.STARTUP,
            snapshot=facts.broker_snapshot,
            operational_ledger=facts.operational_ledger,
            binding=binding,
            final_facts=lambda _account: object(),  # type: ignore[return-value]
        )

    changed_snapshot = replace(facts.broker_snapshot, snapshot_id="changed-snapshot")
    with pytest.raises(RecoveryContradiction):
        _fake_coordinator().reconcile(
            kind=ReconciliationKind.STARTUP,
            snapshot=facts.broker_snapshot,
            operational_ledger=facts.operational_ledger,
            binding=binding,
            final_facts=lambda _account: replace(facts, broker_snapshot=changed_snapshot),
        )

    changed_ledger = replace(facts.operational_ledger, unresolved_execution_ids=("exec-x",))
    with pytest.raises(RecoveryContradiction):
        _fake_coordinator().reconcile(
            kind=ReconciliationKind.STARTUP,
            snapshot=facts.broker_snapshot,
            operational_ledger=facts.operational_ledger,
            binding=binding,
            final_facts=lambda _account: replace(facts, operational_ledger=changed_ledger),
        )

    with pytest.raises(RecoveryContradiction):
        _fake_coordinator(_FakeAccounts(commit_result="c" * 64)).reconcile(
            kind=ReconciliationKind.STARTUP,
            snapshot=facts.broker_snapshot,
            operational_ledger=facts.operational_ledger,
            binding=binding,
            final_facts=lambda _account: facts,
        )


def _seeded_account():
    account = open_buy_account()
    identity = StrategyIdentity.locked()
    account.code_hash = identity.economic_code_fingerprint
    account.data_hash = "d" * 64
    account.data_hash_as_of = "2026-01-05"
    account.data_hash_symbols = ["sz300308"]
    return account


def _runtime_repository(tmp_path: Path, *, clock=lambda: NOW):
    database = Database.open(tmp_path / "firmquant.sqlite3")
    state_path = tmp_path / "uquant-account.json"
    store = UquantAccountStateStore()
    store.save(_seeded_account(), state_path)
    repository = RuntimeAccountRepository(database=database, path=state_path, clock=clock)
    return database, state_path, store, repository


def test_runtime_account_repository_rejects_invalid_ports_clock_and_snapshot(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "constructor.sqlite3")
    try:
        with pytest.raises(TypeError):
            RuntimeAccountRepository(
                database=object(),  # type: ignore[arg-type]
                path=tmp_path / "a.json",
                clock=lambda: NOW,
            )
        with pytest.raises(TypeError):
            RuntimeAccountRepository(
                database=database,
                path="a.json",  # type: ignore[arg-type]
                clock=lambda: NOW,
            )
        with pytest.raises(TypeError):
            RuntimeAccountRepository(
                database=database,
                path=tmp_path / "a.json",
                clock=None,  # type: ignore[arg-type]
            )
    finally:
        database.close()

    database, _path, _store, repository = _runtime_repository(
        tmp_path / "runtime", clock=lambda: datetime(2026, 1, 6, 3)
    )
    try:
        with pytest.raises(RuntimeError):
            repository._now()
        with pytest.raises(TypeError):
            repository.prepare_broker_snapshot(object())  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            repository.commit_broker_snapshot(object())  # type: ignore[arg-type]
    finally:
        database.close()


def test_runtime_account_loader_and_path_identity_fail_closed(monkeypatch, tmp_path: Path) -> None:
    class _Module:
        @staticmethod
        def load_account(_path, *, require_hashes, allow_legacy_schema):
            assert require_hashes is True
            assert allow_legacy_schema is False
            return object()

    monkeypatch.setattr(runtime_account_module.importlib, "import_module", lambda _name: _Module())
    with pytest.raises(RuntimeError):
        runtime_account_module._load_account(tmp_path / "ignored.json")

    with pytest.raises(RecoveryContradiction):
        runtime_account_module._path_sha256(tmp_path / "missing.json")


def test_runtime_account_commit_rejects_mutated_preparation(tmp_path: Path) -> None:
    database, _path, _store, repository = _runtime_repository(tmp_path)
    try:
        prepared = repository.prepare_broker_snapshot(completed_buy_snapshot())
        prepared.prepared_account.cash -= 1.0
        with pytest.raises(RecoveryContradiction):
            repository.commit_broker_snapshot(prepared)
        assert database.scalar("SELECT count(*) FROM account_operations") == 0
    finally:
        database.close()


def test_runtime_account_existing_operation_detects_collision_and_invalid_stages(tmp_path: Path) -> None:
    database, state_path, _store, repository = _runtime_repository(tmp_path)
    try:
        prepared = repository.prepare_broker_snapshot(completed_buy_snapshot())
        operation_id = repository._operation_id(prepared)
        path_sha256 = runtime_account_module._path_sha256(state_path)
        expected_payload = canonical_json(
            {
                "schema": "firmquant.account-operation.v1",
                "operation_kind": "BROKER_SYNC",
                "account_path_sha256": path_sha256,
                "evidence_sha256": prepared.broker_snapshot_sha256,
            }
        )

        def insert_operation(
            *,
            operation_kind: str = "BROKER_SYNC",
            stage: str = "PREPARED",
            actual: str | None = None,
            payload_json: str = expected_payload,
        ) -> None:
            with database.transaction():
                database.write("DELETE FROM account_operations WHERE operation_id = ?", (operation_id,))
                database.write(
                    """
                    INSERT INTO account_operations(
                        operation_id, operation_kind, stage, account_before_sha256,
                        expected_account_after_sha256, actual_account_after_sha256,
                        payload_json, payload_sha256, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        operation_id,
                        operation_kind,
                        stage,
                        prepared.account_before_sha256,
                        prepared.account_after_sha256,
                        actual,
                        payload_json,
                        hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
                        NOW.isoformat(),
                        NOW.isoformat(),
                    ),
                )

        insert_operation(operation_kind="OTHER")
        with pytest.raises(PersistenceConflict):
            repository._existing_broker_operation(prepared, operation_id=operation_id)

        insert_operation(payload_json="{}")
        with pytest.raises(PersistenceConflict):
            repository._existing_broker_operation(prepared, operation_id=operation_id)

        insert_operation(stage="CONTRADICTION")
        with pytest.raises(RecoveryContradiction):
            repository._existing_broker_operation(prepared, operation_id=operation_id)

        insert_operation(stage="PREPARED", actual=prepared.account_after_sha256)
        with pytest.raises(RecoveryContradiction):
            repository._existing_broker_operation(prepared, operation_id=operation_id)

        insert_operation(stage="FILE_COMMITTED", actual="0" * 64)
        with pytest.raises(RecoveryContradiction):
            repository._existing_broker_operation(prepared, operation_id=operation_id)

        insert_operation(stage="RECEIPT_COMMITTED", actual=prepared.account_after_sha256)
        operation = repository._existing_broker_operation(prepared, operation_id=operation_id)
        assert operation is not None
        assert operation.expected_account_after_sha256 == prepared.account_after_sha256
    finally:
        database.close()
