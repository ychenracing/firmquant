from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from firmquant.domain.broker_facts import AccountType, BrokerOrderStatus
from firmquant.domain.values import Money, Shares
from firmquant.persistence.database import Database
from firmquant.reconciliation.models import ReconciliationKind
from firmquant.reconciliation.service import ReconciliationService
from tests.fixtures.reconciliation_cases import (
    NOW,
    healthy_reconciliation_facts,
    with_strategy_cash,
)


@pytest.fixture
def database(tmp_path: Path):
    opened = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        yield opened
    finally:
        opened.close()


def service(database: Database, *, tolerance: str = "0.01") -> ReconciliationService:
    return ReconciliationService(
        database=database,
        cash_tolerance=Money(Decimal(tolerance)),
        clock=lambda: NOW,
    )


def test_healthy_reconciliation_is_append_only_and_audited(database: Database) -> None:
    receipt = service(database).run(
        ReconciliationKind.STARTUP,
        healthy_reconciliation_facts(),
    )

    assert receipt.passed is True
    assert receipt.halt_required is False
    assert receipt.blockers == ()
    assert database.scalar("SELECT count(*) FROM reconciliation_runs") == 1
    assert database.scalar("SELECT count(*) FROM audit_events") == 1
    with pytest.raises(sqlite3.IntegrityError, match="append-only"), database.transaction():
        database.write(
            "UPDATE reconciliation_runs SET passed = 0 WHERE reconciliation_id = ?",
            (receipt.reconciliation_id,),
        )


def test_cash_tolerance_is_decimal_and_never_applies_to_shares(database: Database) -> None:
    inside = service(database).run(
        ReconciliationKind.INTRADAY,
        with_strategy_cash("994.89"),
    )
    outside = service(database).run(
        ReconciliationKind.EOD,
        with_strategy_cash("994.8899"),
    )

    assert inside.passed is True
    assert outside.passed is False
    assert "AVAILABLE_CASH_MISMATCH" in outside.blockers


@pytest.mark.parametrize(
    ("field", "value", "blocker"),
    [
        ("total_shares", Shares(99), "POSITION_SHARE_MISMATCH"),
        ("sellable_shares", Shares(1), "SELLABLE_SHARE_MISMATCH"),
    ],
)
def test_position_and_sellable_differences_are_zero_tolerance(
    database: Database,
    field: str,
    value: Shares,
    blocker: str,
) -> None:
    facts = healthy_reconciliation_facts()
    expected = facts.strategy_account.positions[0]
    changed = replace(expected, **{field: value})
    facts = replace(
        facts,
        strategy_account=replace(facts.strategy_account, positions=(changed,)),
    )

    receipt = service(database).run(ReconciliationKind.STARTUP, facts)

    assert receipt.halt_required is True
    assert blocker in receipt.blockers
    assert "UNEXPLAINED_POSITION_CHANGE" in receipt.blockers


def test_account_identity_or_type_change_halts(database: Database) -> None:
    facts = healthy_reconciliation_facts()
    changed = replace(
        facts,
        operational_ledger=replace(
            facts.operational_ledger,
            expected_account_id_hash="b" * 64,
            expected_account_type=AccountType.MARGIN,
        ),
    )

    receipt = service(database).run(ReconciliationKind.STARTUP, changed)

    assert "ACCOUNT_IDENTITY_CHANGED" in receipt.blockers
    assert "ACCOUNT_TYPE_CHANGED" in receipt.blockers


def test_unknown_fill_and_unknown_local_state_halt(database: Database) -> None:
    facts = healthy_reconciliation_facts()
    fill = replace(
        facts.broker_snapshot.fills[0],
        broker_fill_id="unmapped-fill",
        broker_order_id="unmapped-order",
        raw_payload_sha256="9" * 64,
    )
    changed = replace(
        facts,
        broker_snapshot=replace(
            facts.broker_snapshot,
            fills=(*facts.broker_snapshot.fills, fill),
            raw_payload_sha256="8" * 64,
        ),
        operational_ledger=replace(
            facts.operational_ledger,
            unresolved_execution_ids=("exec_unknown",),
            submitting_unresolved_execution_ids=("exec_submitting",),
        ),
    )

    receipt = service(database).run(ReconciliationKind.RECOVERY, changed)

    assert "UNMAPPED_BROKER_FILL" in receipt.blockers
    assert "UNRESOLVED_LOCAL_ORDER" in receipt.blockers
    assert "SUBMITTING_UNRESOLVED" in receipt.blockers
    assert "INVESTIGATE_UNKNOWN_BROKER_ACTIVITY" in receipt.operator_actions


def test_local_terminal_broker_active_contradiction_halts(database: Database) -> None:
    facts = healthy_reconciliation_facts()
    broker_order = replace(
        facts.broker_snapshot.orders[0],
        status=BrokerOrderStatus.ACKNOWLEDGED,
        filled_shares=Shares(0),
    )
    changed = replace(
        facts,
        broker_snapshot=replace(
            facts.broker_snapshot,
            orders=(broker_order,),
            fills=(),
            raw_payload_sha256="7" * 64,
        ),
        operational_ledger=replace(
            facts.operational_ledger,
            known_broker_fill_ids=frozenset(),
        ),
    )

    receipt = service(database).run(ReconciliationKind.STARTUP, changed)

    assert "LOCAL_TERMINAL_BROKER_ACTIVE" in receipt.blockers
    assert "ORDER_FILLED_SHARES_MISMATCH" in receipt.blockers


@pytest.mark.parametrize(
    ("changes", "blocker"),
    [
        ({"uquant_code_identity_matches": False}, "UQUANT_CODE_IDENTITY_DRIFT"),
        ({"data_identity_matches": False}, "DATA_HISTORY_REWRITE"),
        ({"config_identity_matches": False}, "CONFIG_IDENTITY_DRIFT"),
    ],
)
def test_identity_drift_halts(database: Database, changes: dict[str, object], blocker: str) -> None:
    receipt = service(database).run(
        ReconciliationKind.MANUAL,
        replace(healthy_reconciliation_facts(), **changes),
    )

    assert blocker in receipt.blockers


def test_company_action_is_never_guessed_or_auto_adopted(database: Database) -> None:
    facts = healthy_reconciliation_facts()
    symbol = facts.broker_snapshot.positions[0].symbol
    changed = replace(
        facts,
        company_action_suspected_symbols=frozenset({symbol}),
    )

    receipt = service(database).run(ReconciliationKind.EOD, changed)

    assert "CORPORATE_ACTION_SUSPECTED" in receipt.blockers
    assert "VERIFY_COMPANY_ACTION_WITH_BROKER" in receipt.operator_actions


def test_receipt_rejects_naive_clock_before_writing(database: Database) -> None:
    unsafe_service = ReconciliationService(
        database=database,
        cash_tolerance=Money(Decimal("0.01")),
        clock=lambda: datetime(2026, 1, 6, 8, 5),
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        unsafe_service.run(ReconciliationKind.STARTUP, healthy_reconciliation_facts())

    assert database.scalar("SELECT count(*) FROM reconciliation_runs") == 0
