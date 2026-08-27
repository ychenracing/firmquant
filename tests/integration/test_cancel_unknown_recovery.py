from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from firmquant.domain.broker_facts import BrokerOrderStatus
from firmquant.domain.orders import OrderState
from firmquant.persistence.database import Database
from firmquant.persistence.production_recovery import ProductionRecoveryService
from firmquant.persistence.production_repository import MonotonicExecutionLedgerRepository
from tests.fixtures.recovery_cases import (
    NOW,
    broker_fill,
    broker_order,
    create_submitting_case,
    fake_recovery_broker,
)


@pytest.fixture
def database(tmp_path: Path):
    opened = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        yield opened
    finally:
        opened.close()


def _cancel_unknown(database: Database):
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
        unknown = repository.mark_attempt_unknown(
            cancelling,
            cancel_attempt,
            diagnostic_code="CANCEL_CALL_OUTCOME_UNKNOWN",
            occurred_at=NOW,
        )
    assert unknown.state is OrderState.UNKNOWN
    return case, repository, cancel_attempt, acknowledged


def _recover(database: Database, broker, *, offset: int = 1):
    return ProductionRecoveryService(
        database=database,
        account_store=None,
        account_path=None,
        gateway=broker,
        clock=lambda: NOW + timedelta(seconds=offset),
    ).recover()


@pytest.mark.parametrize(
    ("status", "filled_shares", "expected"),
    [
        (BrokerOrderStatus.CANCELLED, 0, OrderState.CANCELLED),
        (BrokerOrderStatus.FILLED, 100, OrderState.FILLED),
        (BrokerOrderStatus.REJECTED, 0, OrderState.REJECTED),
        (BrokerOrderStatus.EXPIRED, 0, OrderState.EXPIRED),
    ],
)
def test_cancel_unknown_resolves_only_from_terminal_broker_truth_without_write(
    database: Database,
    status: BrokerOrderStatus,
    filled_shares: int,
    expected: OrderState,
) -> None:
    case, repository, _, acknowledged = _cancel_unknown(database)
    fact = replace(
        acknowledged,
        status=status,
        filled_shares=type(acknowledged.filled_shares)(filled_shares),
        event_sequence=22,
    )
    fills = (
        (broker_fill(case.command, shares=100, sequence=21, fill_id="cancel-unknown-fill"),)
        if filled_shares
        else ()
    )
    broker = fake_recovery_broker(orders=(fact,), fills=fills)

    report = _recover(database, broker)
    recovered = repository.load(case.aggregate.intent.execution_id)

    assert recovered is not None and recovered.state is expected
    assert recovered.filled_shares.value == filled_shares
    assert report.unresolved_order_ids == ()
    assert broker.submitted_commands == ()
    assert broker.cancelled_order_ids == ()


def test_cancel_unknown_with_only_active_broker_fact_remains_halted_without_write(
    database: Database,
) -> None:
    case, repository, _, acknowledged = _cancel_unknown(database)
    active = replace(acknowledged, event_sequence=22)
    broker = fake_recovery_broker(orders=(active,))

    first = _recover(database, broker)
    second = _recover(database, broker, offset=2)
    recovered = repository.load(case.aggregate.intent.execution_id)

    assert recovered is not None and recovered.state is OrderState.UNKNOWN
    assert first.halt_required is True
    assert second.halt_required is True
    assert first.unresolved_order_ids == (case.aggregate.intent.execution_id,)
    assert second.unresolved_order_ids == first.unresolved_order_ids
    assert "BROKER_RECOVERY_CONTRADICTION" not in second.blockers
    assert broker.submitted_commands == ()
    assert broker.cancelled_order_ids == ()
