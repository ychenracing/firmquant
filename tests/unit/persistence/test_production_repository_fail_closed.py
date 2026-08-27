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


def _repository_case(database: Database):
    case = create_submitting_case(database)
    repository = MonotonicExecutionLedgerRepository(database)
    aggregate = repository.load(case.aggregate.intent.execution_id)
    assert aggregate is not None
    return case, repository, aggregate


def test_same_order_sequence_cannot_be_reused_for_different_broker_truth(
    database: Database,
) -> None:
    case, repository, aggregate = _repository_case(database)
    acknowledged = broker_order(case.command, sequence=20)
    with database.transaction():
        current = repository.record_submit_result(
            aggregate,
            case.attempt,
            acknowledged,
            (),
            received_at=NOW,
        )

    contradictory = replace(
        acknowledged,
        status=BrokerOrderStatus.CANCELLED,
        event_sequence=20,
    )
    with database.transaction(), pytest.raises(PersistenceConflict, match="sequence was reused"):
        repository.reconcile_broker_fact(
            current,
            contradictory,
            (),
            received_at=NOW,
        )


def test_same_attempt_cannot_change_its_returned_broker_response(
    database: Database,
) -> None:
    case, repository, aggregate = _repository_case(database)
    acknowledged = broker_order(case.command, sequence=20)
    with database.transaction():
        current = repository.record_submit_result(
            aggregate,
            case.attempt,
            acknowledged,
            (),
            received_at=NOW,
        )

    changed_response = replace(acknowledged, event_sequence=21)
    with database.transaction(), pytest.raises(PersistenceConflict, match="response changed"):
        repository.record_submit_result(
            current,
            case.attempt,
            changed_response,
            (),
            received_at=NOW,
        )

    assert database.scalar("SELECT count(*) FROM broker_responses") == 1


def test_exact_same_attempt_response_replay_is_idempotent(database: Database) -> None:
    case, repository, aggregate = _repository_case(database)
    acknowledged = broker_order(case.command, sequence=20)
    with database.transaction():
        first = repository.record_submit_result(
            aggregate,
            case.attempt,
            acknowledged,
            (),
            received_at=NOW,
        )
    with database.transaction():
        second = repository.record_submit_result(
            first,
            case.attempt,
            acknowledged,
            (),
            received_at=NOW,
        )

    assert second.state is OrderState.ACKNOWLEDGED
    assert second.version == first.version
    assert database.scalar("SELECT count(*) FROM broker_responses") == 1


def test_filled_status_requires_full_cumulative_fill(database: Database) -> None:
    case, repository, aggregate = _repository_case(database)
    contradictory = broker_order(
        case.command,
        status=BrokerOrderStatus.FILLED,
        filled_shares=50,
        sequence=22,
    )

    with database.transaction(), pytest.raises(PersistenceConflict, match="FILLED broker order"):
        repository.reconcile_broker_fact(
            aggregate,
            contradictory,
            (),
            received_at=NOW,
        )


@pytest.mark.parametrize("filled_shares", [0, 100])
def test_partial_status_requires_strictly_partial_cumulative_fill(
    database: Database,
    filled_shares: int,
) -> None:
    case, repository, aggregate = _repository_case(database)
    contradictory = broker_order(
        case.command,
        status=BrokerOrderStatus.PARTIALLY_FILLED,
        filled_shares=filled_shares,
        sequence=22,
    )

    with database.transaction(), pytest.raises(PersistenceConflict, match="PARTIALLY_FILLED broker order"):
        repository.reconcile_broker_fact(
            aggregate,
            contradictory,
            (),
            received_at=NOW,
        )


def test_duplicate_fill_in_one_snapshot_is_deduplicated_by_economic_identity(
    database: Database,
) -> None:
    case, repository, aggregate = _repository_case(database)
    fill = broker_fill(case.command, shares=50, sequence=21)
    duplicate = replace(fill, raw_payload_sha256="9" * 64)
    partial = broker_order(
        case.command,
        status=BrokerOrderStatus.PARTIALLY_FILLED,
        filled_shares=50,
        sequence=22,
    )

    with database.transaction():
        current = repository.reconcile_broker_fact(
            aggregate,
            partial,
            (fill, duplicate),
            received_at=NOW,
        )

    assert current.filled_shares.value == 50
    assert len(current.fills) == 1
    assert database.scalar("SELECT count(*) FROM fills") == 1


def test_duplicate_fill_in_one_snapshot_with_conflicting_economics_fails_closed(
    database: Database,
) -> None:
    case, repository, aggregate = _repository_case(database)
    fill = broker_fill(case.command, shares=50, sequence=21)
    conflicting = replace(fill, commission=Money(Decimal("6.00")))
    partial = broker_order(
        case.command,
        status=BrokerOrderStatus.PARTIALLY_FILLED,
        filled_shares=50,
        sequence=22,
    )

    with database.transaction(), pytest.raises(PersistenceConflict, match="fill identity collision"):
        repository.reconcile_broker_fact(
            aggregate,
            partial,
            (fill, conflicting),
            received_at=NOW,
        )


def test_zero_execution_sequence_fill_fails_closed(database: Database) -> None:
    case, repository, aggregate = _repository_case(database)
    fill = replace(broker_fill(case.command, shares=50, sequence=21), event_sequence=0)
    partial = broker_order(
        case.command,
        status=BrokerOrderStatus.PARTIALLY_FILLED,
        filled_shares=50,
        sequence=22,
    )

    with database.transaction(), pytest.raises(PersistenceConflict, match="fill contradicts"):
        repository.reconcile_broker_fact(
            aggregate,
            partial,
            (fill,),
            received_at=NOW,
        )
