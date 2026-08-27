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
    ("status", "expected"),
    [
        (BrokerOrderStatus.REJECTED, OrderState.REJECTED),
        (BrokerOrderStatus.EXPIRED, OrderState.EXPIRED),
    ],
)
def test_submit_terminal_result_without_fill_preserves_true_terminal(
    database: Database,
    status: BrokerOrderStatus,
    expected: OrderState,
) -> None:
    case = create_submitting_case(database)
    repository = MonotonicExecutionLedgerRepository(database)
    current = repository.load(case.aggregate.intent.execution_id)
    assert current is not None
    fact = broker_order(case.command, status=status, sequence=22)

    with database.transaction():
        result = repository.record_submit_result(
            current,
            case.attempt,
            fact,
            (),
            received_at=NOW,
        )

    assert result.state is expected
    assert result.filled_shares.value == 0
    assert database.scalar(
        "SELECT status FROM broker_orders WHERE broker_order_id = ?",
        (fact.broker_order_id,),
    ) == status.value


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (BrokerOrderStatus.REJECTED, OrderState.REJECTED),
        (BrokerOrderStatus.EXPIRED, OrderState.EXPIRED),
    ],
)
def test_submit_partial_terminal_result_imports_confirmed_fill_first(
    database: Database,
    status: BrokerOrderStatus,
    expected: OrderState,
) -> None:
    case = create_submitting_case(database)
    repository = MonotonicExecutionLedgerRepository(database)
    current = repository.load(case.aggregate.intent.execution_id)
    assert current is not None
    fill = broker_fill(case.command, shares=50, sequence=21)
    fact = broker_order(case.command, status=status, filled_shares=50, sequence=22)

    with database.transaction():
        result = repository.record_submit_result(
            current,
            case.attempt,
            fact,
            (fill,),
            received_at=NOW,
        )

    assert result.state is expected
    assert result.filled_shares.value == 50
    assert len(result.fills) == 1
    assert database.scalar("SELECT count(*) FROM fills") == 1


def test_submit_unknown_imports_confirmed_fill_and_remains_unresolved(database: Database) -> None:
    case = create_submitting_case(database)
    repository = MonotonicExecutionLedgerRepository(database)
    current = repository.load(case.aggregate.intent.execution_id)
    assert current is not None
    fill = broker_fill(case.command, shares=50, sequence=21)
    unknown = broker_order(
        case.command,
        status=BrokerOrderStatus.UNKNOWN,
        filled_shares=50,
        sequence=22,
    )

    with database.transaction():
        result = repository.record_submit_result(
            current,
            case.attempt,
            unknown,
            (fill,),
            received_at=NOW,
        )

    assert result.state is OrderState.UNKNOWN
    assert result.filled_shares.value == 50
    assert database.scalar(
        "SELECT state FROM broker_order_attempts WHERE attempt_id = ?",
        (case.attempt.attempt_id,),
    ) == "UNKNOWN"


def test_cancel_unknown_imports_confirmed_fill_and_remains_unresolved(database: Database) -> None:
    case = create_submitting_case(database)
    repository = MonotonicExecutionLedgerRepository(database)
    current = repository.load(case.aggregate.intent.execution_id)
    assert current is not None
    acknowledged = broker_order(case.command, sequence=20)
    with database.transaction():
        current = repository.record_submit_result(
            current,
            case.attempt,
            acknowledged,
            (),
            received_at=NOW,
        )
        cancelling, cancel_attempt = repository.begin_cancel(current, started_at=NOW)

    fill = broker_fill(case.command, shares=50, sequence=21)
    unknown = broker_order(
        case.command,
        status=BrokerOrderStatus.UNKNOWN,
        filled_shares=50,
        sequence=22,
    )
    with database.transaction():
        result = repository.record_cancel_result(
            cancelling,
            cancel_attempt,
            unknown,
            (fill,),
            received_at=NOW,
        )

    assert result.state is OrderState.UNKNOWN
    assert result.filled_shares.value == 50
    assert database.scalar(
        "SELECT state FROM broker_order_attempts WHERE attempt_id = ?",
        (cancel_attempt.attempt_id,),
    ) == "UNKNOWN"


def test_cancel_duplicate_fill_id_with_conflicting_economics_fails_closed(database: Database) -> None:
    case = create_submitting_case(database)
    repository = MonotonicExecutionLedgerRepository(database)
    current = repository.load(case.aggregate.intent.execution_id)
    assert current is not None
    fill = broker_fill(case.command, shares=50, sequence=21)
    partial = broker_order(
        case.command,
        status=BrokerOrderStatus.PARTIALLY_FILLED,
        filled_shares=50,
        sequence=22,
    )
    with database.transaction():
        current = repository.record_submit_result(
            current,
            case.attempt,
            partial,
            (fill,),
            received_at=NOW,
        )
        cancelling, cancel_attempt = repository.begin_cancel(current, started_at=NOW)

    conflicting = replace(fill, commission=Money(Decimal("6.00")))
    cancelled = broker_order(
        case.command,
        status=BrokerOrderStatus.CANCELLED,
        filled_shares=50,
        sequence=23,
    )
    with database.transaction(), pytest.raises(PersistenceConflict, match="fill identity collision"):
        repository.record_cancel_result(
            cancelling,
            cancel_attempt,
            cancelled,
            (conflicting,),
            received_at=NOW,
        )
