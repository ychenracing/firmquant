from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from uquant.types import AccountState

from firmquant.application.composition import ConfiguredOperatorPorts
from firmquant.application.operations import OperatorCommandDenied
from firmquant.domain.broker_facts import AccountType
from firmquant.domain.values import Money, Symbol
from firmquant.persistence.account_authority import AdjustmentCoverage
from firmquant.persistence.database import Database
from firmquant.reconciliation.account_coordinator import (
    AccountReconciliationBlocked,
    AccountReconciliationCoordinator,
)
from firmquant.reconciliation.account_preflight import (
    AccountPreflightResult,
    account_difference_sha256,
    evaluate_account_preflight,
)
from firmquant.strategy.account_bootstrap import (
    AccountBootstrapDenied,
    AccountBootstrapService,
    BootstrapDataIdentity,
)
from firmquant.strategy.identity import StrategyIdentity
from tests.fixtures.broker_snapshots import completed_buy_snapshot
from tests.fixtures.reconciliation_cases import NOW, healthy_reconciliation_facts
from tests.integration.test_cli_operations import paper_config


def _data_identity(_snapshot) -> BootstrapDataIdentity:
    return BootstrapDataIdentity(
        data_hash="d" * 64,
        as_of="2026-01-06",
        symbols=("sz300308",),
    )


def _empty_snapshot():
    snapshot = completed_buy_snapshot()
    return replace(
        snapshot,
        snapshot_id="bootstrap-edge-empty",
        account=replace(
            snapshot.account,
            available_cash=Money(Decimal("1000")),
            total_assets=Money(Decimal("1000")),
        ),
        positions=(),
        orders=(),
        fills=(),
        raw_payload_sha256="a" * 64,
    )


def _service(
    tmp_path: Path,
    database: Database,
    *,
    clock=lambda: NOW,
    provider=_data_identity,
) -> AccountBootstrapService:
    return AccountBootstrapService(
        database=database,
        account_path=tmp_path / "uquant-account.json",
        data_identity_provider=provider,
        clock=clock,
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"data_hash": "x" * 64, "as_of": "2026-01-06", "symbols": ("sz300308",)}, "hash"),
        ({"data_hash": "d" * 64, "as_of": "", "symbols": ("sz300308",)}, "as-of"),
        ({"data_hash": "d" * 64, "as_of": "2026-01-06", "symbols": ()}, "non-empty"),
        (
            {
                "data_hash": "d" * 64,
                "as_of": "2026-01-06",
                "symbols": ("sz300308", "sz300308"),
            },
            "sorted and unique",
        ),
    ],
)
def test_bootstrap_data_identity_rejects_noncanonical_values(kwargs, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        BootstrapDataIdentity(**kwargs)


def test_bootstrap_service_constructor_rejects_invalid_ports(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        with pytest.raises(TypeError, match="requires Database"):
            AccountBootstrapService(
                database=object(),  # type: ignore[arg-type]
                account_path=tmp_path / "a.json",
                data_identity_provider=_data_identity,
                clock=lambda: NOW,
            )
        with pytest.raises(TypeError, match="path must be Path"):
            AccountBootstrapService(
                database=database,
                account_path="bad",  # type: ignore[arg-type]
                data_identity_provider=_data_identity,
                clock=lambda: NOW,
            )
        with pytest.raises(TypeError, match="providers must be callable"):
            AccountBootstrapService(
                database=database,
                account_path=tmp_path / "a.json",
                data_identity_provider=None,  # type: ignore[arg-type]
                clock=lambda: NOW,
            )
    finally:
        database.close()


def test_bootstrap_rejects_bad_clock_and_existing_unbound_state(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        naive = datetime(2026, 1, 6, 3)
        service = _service(tmp_path, database, clock=lambda: naive)
        with pytest.raises(AccountBootstrapDenied, match="CLOCK_UNAVAILABLE"):
            service._now()

        account_path = tmp_path / "uquant-account.json"
        account_path.write_text("{}", encoding="utf-8")
        service = _service(tmp_path, database)
        with pytest.raises(AccountBootstrapDenied, match="UNBOUND_ACCOUNT_STATE_PRESENT"):
            service.bootstrap(_empty_snapshot())
    finally:
        database.close()


def test_bootstrap_rejects_non_disarmed_runtime_and_invalid_summary(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        with database.transaction():
            database.write(
                """
                INSERT INTO runtime_state(singleton_id, mode, state, revision, reason, blockers_json, updated_at)
                VALUES (1, 'PAPER', 'READY', 0, 'test', '[]', ?)
                """,
                (NOW.isoformat(),),
            )
        service = _service(tmp_path, database)
        with pytest.raises(AccountBootstrapDenied, match="RUNTIME_NOT_DISARMED"):
            service.bootstrap(_empty_snapshot())
        with database.transaction():
            database.write("DELETE FROM runtime_state WHERE singleton_id = 1")

        snapshot = _empty_snapshot()
        broken = replace(
            snapshot,
            account=replace(snapshot.account, total_assets=Money(Decimal("1001"))),
        )
        with pytest.raises(AccountBootstrapDenied, match="BROKER_ECONOMIC_SUMMARY_INVALID"):
            service.bootstrap(broken)
    finally:
        database.close()


def test_bootstrap_rejects_snapshot_type_account_type_activity_and_bad_data_identity(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        service = _service(tmp_path, database)
        with pytest.raises(AccountBootstrapDenied, match="BROKER_SNAPSHOT_INVALID"):
            service.bootstrap(object())  # type: ignore[arg-type]

        snapshot = _empty_snapshot()
        margin = replace(snapshot, account=replace(snapshot.account, account_type=AccountType.MARGIN))
        with pytest.raises(AccountBootstrapDenied, match="ACCOUNT_TYPE_UNSUPPORTED"):
            service.bootstrap(margin)

        activity = replace(snapshot, orders=completed_buy_snapshot().orders)
        with pytest.raises(AccountBootstrapDenied, match="BROKER_ACTIVITY_PRESENT"):
            service.bootstrap(activity)

        bad_provider = _service(tmp_path, database, provider=lambda _snapshot: object())
        with pytest.raises(AccountBootstrapDenied, match="DATA_IDENTITY_INVALID"):
            bad_provider.bootstrap(snapshot)
    finally:
        database.close()


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (Decimal("0"), "ACCOUNT_CASH_INVALID"),
        (Decimal("0.1000000000000000000001"), "ACCOUNT_CASH_PRECISION_INVALID"),
        (Decimal("NaN"), "ACCOUNT_CASH_PRECISION_INVALID"),
    ],
)
def test_bootstrap_cash_boundary_rejects_unsafe_values(value: Decimal, reason: str) -> None:
    with pytest.raises(AccountBootstrapDenied, match=reason):
        AccountBootstrapService._cash_float(value)


def test_bootstrap_seed_validation_distinguishes_code_data_cash_and_pending_order() -> None:
    snapshot = _empty_snapshot()
    identity = StrategyIdentity.locked()
    data = _data_identity(snapshot)

    seed = AccountState.empty(1000.0)
    with pytest.raises(AccountBootstrapDenied, match="SEED_CODE_IDENTITY_MISMATCH"):
        AccountBootstrapService._validate_seed(seed, snapshot=snapshot, identity=identity, data=data)

    seed.code_hash = identity.economic_code_fingerprint
    with pytest.raises(AccountBootstrapDenied, match="SEED_DATA_IDENTITY_MISMATCH"):
        AccountBootstrapService._validate_seed(seed, snapshot=snapshot, identity=identity, data=data)

    seed.data_hash = data.data_hash
    seed.data_hash_as_of = data.as_of
    seed.data_hash_symbols = list(data.symbols)
    seed.cash = 999.0
    with pytest.raises(AccountBootstrapDenied, match="SEED_CASH_MISMATCH"):
        AccountBootstrapService._validate_seed(seed, snapshot=snapshot, identity=identity, data=data)

    seed.cash = 1000.0
    seed.pending_orders.append(SimpleNamespace(order_id="manual"))
    with pytest.raises(AccountBootstrapDenied, match="SEED_PENDING_ORDER_UNOWNED"):
        AccountBootstrapService._validate_seed(seed, snapshot=snapshot, identity=identity, data=data)


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda args: {**args, "account_id_hash": "x" * 64}, "account identity"),
        (lambda args: {**args, "broker_snapshot_sha256": "x" * 64}, "broker snapshot"),
        (lambda args: {**args, "session": "2026-01-06"}, "session must be date"),
        (lambda args: {**args, "coverage": "cash"}, "coverage must be typed"),
        (lambda args: {**args, "expected": Decimal("-1")}, "nonnegative"),
    ],
)
def test_account_difference_identity_rejects_ambiguous_inputs(mutator, message: str) -> None:
    args = {
        "account_id_hash": "a" * 64,
        "symbol": None,
        "session": date(2026, 1, 6),
        "coverage": AdjustmentCoverage.AVAILABLE_CASH,
        "broker_snapshot_sha256": "b" * 64,
        "expected": Decimal("1"),
        "observed": Decimal("2"),
    }
    with pytest.raises((TypeError, ValueError), match=message):
        account_difference_sha256(**mutator(args))


def test_account_difference_identity_enforces_symbol_scope() -> None:
    common = {
        "account_id_hash": "a" * 64,
        "session": date(2026, 1, 6),
        "broker_snapshot_sha256": "b" * 64,
        "expected": 1,
        "observed": 2,
    }
    with pytest.raises(ValueError, match="requires symbol"):
        account_difference_sha256(
            **common,
            symbol=None,
            coverage=AdjustmentCoverage.POSITION_TOTAL_SHARES,
        )
    with pytest.raises(ValueError, match="must not carry a symbol"):
        account_difference_sha256(
            **common,
            symbol=Symbol.parse("sz300308"),
            coverage=AdjustmentCoverage.AVAILABLE_CASH,
        )


def test_preflight_result_and_input_validation_are_fail_closed() -> None:
    with pytest.raises(ValueError, match="sorted and unique"):
        AccountPreflightResult(passed=False, blockers=("B", "A"), explained_fill_ids=())
    with pytest.raises(ValueError, match="contradicts blockers"):
        AccountPreflightResult(passed=True, blockers=("A",), explained_fill_ids=())

    facts = healthy_reconciliation_facts()
    with pytest.raises(TypeError, match="snapshot must be BrokerSnapshot"):
        evaluate_account_preflight(
            snapshot=object(),  # type: ignore[arg-type]
            account=object(),
            operational_ledger=facts.operational_ledger,
            binding=object(),  # type: ignore[arg-type]
            cash_tolerance=Money(Decimal("0.01")),
        )


def test_coordinator_constructor_and_blocker_validation_are_fail_closed() -> None:
    with pytest.raises(TypeError, match="cash tolerance must be Decimal"):
        AccountReconciliationCoordinator(
            account_repository=object(),  # type: ignore[arg-type]
            reconciler=object(),  # type: ignore[arg-type]
            cash_tolerance=0.01,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="finite and nonnegative"):
        AccountReconciliationCoordinator(
            account_repository=object(),  # type: ignore[arg-type]
            reconciler=object(),  # type: ignore[arg-type]
            cash_tolerance=Decimal("-0.01"),
        )
    with pytest.raises(ValueError, match="sorted and unique"):
        AccountReconciliationBlocked(("B", "A"))


def test_configured_bootstrap_port_rejects_invalid_seed_and_nonproduction_mode(tmp_path: Path) -> None:
    config = tmp_path / "firmquant.toml"
    paper_config(config)
    ports = ConfiguredOperatorPorts(config_path=config, clock=lambda: NOW)

    with pytest.raises(OperatorCommandDenied, match="ACCOUNT_STATE_SEED_INVALID"):
        ports.bootstrap_account("bad")  # type: ignore[arg-type]
    with pytest.raises(OperatorCommandDenied, match="ACCOUNT_BOOTSTRAP_REQUIRES_PRODUCTION_BROKER"):
        ports.bootstrap_account(None)
