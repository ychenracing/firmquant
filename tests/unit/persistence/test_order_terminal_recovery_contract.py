from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from firmquant.domain.broker_facts import BrokerOrderStatus
from firmquant.domain.orders import OrderState
from firmquant.domain.values import Money
from firmquant.persistence.database import Database
from firmquant.persistence.production_repository import MonotonicExecutionLedgerRepository
from firmquant.persistence.repositories import PersistenceConflict
from tests.fixtures.recovery_cases import NOW, broker_fill, broker_order, create_submitting_case


@pytest.fixture
def database(tmp_path: Path):
    opened = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        yield opened
    finally:
        opened.close()


@pytest.mark.parametrize(
    ("status", "expected_state"),
    [
        (BrokerOrderStatus.REJECTED, OrderState.REJECTED),
        (BrokerOrderStatus.EXPIRED, OrderState.EXPIRED),
        (BrokerOrderStatus.CANCELLED, OrderState.CANCELLED),
    ],
)
def test_partial_terminal_fact_imports_fill_before_preserving_true_terminal(
    database: Database,
    status: BrokerOrderStatus,
    expected_state: OrderState,
) -> None:
    case = create_submitting_case(database)
    repository = MonotonicExecutionLedgerRepository(database)
    fill = broker_fill(case.command, shares=50, sequence=21)
    terminal = broker_order(
        case.command,
        status=status,
        filled_shares=50,
        sequence=22,
    )
    aggregate = repository.load(case.aggregate.intent.execution_id)
    assert aggregate is not None

    with database.transaction():
        recovered = repository.reconcile_broker_fact(
            aggregate,
            terminal,
            (fill,),
            received_at=NOW,
        )

    assert recovered.state is expected_state
    assert recovered.filled_shares.value == 50
    assert database.scalar("SELECT count(*) FROM fills") == 1
    assert (
        database.scalar(
            "SELECT status FROM broker_orders WHERE broker_order_id = ?",
            (terminal.broker_order_id,),
        )
        == status.value
    )


def test_same_fill_id_and_same_economics_is_idempotent_even_if_raw_payload_differs(
    database: Database,
) -> None:
    case = create_submitting_case(database)
    repository = MonotonicExecutionLedgerRepository(database)
    fill = broker_fill(case.command, shares=50, sequence=21)
    partial = broker_order(
        case.command,
        status=BrokerOrderStatus.PARTIALLY_FILLED,
        filled_shares=50,
        sequence=22,
    )
    aggregate = repository.load(case.aggregate.intent.execution_id)
    assert aggregate is not None

    with database.transaction():
        first = repository.reconcile_broker_fact(
            aggregate,
            partial,
            (fill,),
            received_at=NOW,
        )
    duplicate = replace(fill, raw_payload_sha256="9" * 64)
    with database.transaction():
        second = repository.reconcile_broker_fact(
            first,
            partial,
            (duplicate,),
            received_at=NOW,
        )

    assert second.filled_shares.value == 50
    assert len(second.fills) == 1
    assert database.scalar("SELECT count(*) FROM fills") == 1


def test_same_fill_id_with_different_economics_fails_closed(database: Database) -> None:
    case = create_submitting_case(database)
    repository = MonotonicExecutionLedgerRepository(database)
    fill = broker_fill(case.command, shares=50, sequence=21)
    partial = broker_order(
        case.command,
        status=BrokerOrderStatus.PARTIALLY_FILLED,
        filled_shares=50,
        sequence=22,
    )
    aggregate = repository.load(case.aggregate.intent.execution_id)
    assert aggregate is not None

    with database.transaction():
        current = repository.reconcile_broker_fact(
            aggregate,
            partial,
            (fill,),
            received_at=NOW,
        )

    conflicting = replace(fill, commission=Money(Decimal("6.00")))
    with database.transaction(), pytest.raises(PersistenceConflict, match="fill identity collision"):
        repository.reconcile_broker_fact(
            current,
            partial,
            (conflicting,),
            received_at=NOW,
        )


def test_unseen_fill_with_regressed_execution_sequence_fails_closed(database: Database) -> None:
    case = create_submitting_case(database)
    repository = MonotonicExecutionLedgerRepository(database)
    first_fill = broker_fill(case.command, shares=50, sequence=22, fill_id="fill-newer")
    partial = broker_order(
        case.command,
        status=BrokerOrderStatus.PARTIALLY_FILLED,
        filled_shares=50,
        sequence=30,
    )
    aggregate = repository.load(case.aggregate.intent.execution_id)
    assert aggregate is not None

    with database.transaction():
        current = repository.reconcile_broker_fact(
            aggregate,
            partial,
            (first_fill,),
            received_at=NOW,
        )

    old_fill = broker_fill(case.command, shares=20, sequence=21, fill_id="fill-older-late")
    advanced = broker_order(
        case.command,
        status=BrokerOrderStatus.PARTIALLY_FILLED,
        filled_shares=70,
        sequence=31,
    )
    with database.transaction(), pytest.raises(PersistenceConflict, match="sequence"):
        repository.reconcile_broker_fact(
            current,
            advanced,
            (old_fill, first_fill),
            received_at=NOW,
        )
