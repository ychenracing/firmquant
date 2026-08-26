from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from firmquant.domain.broker_facts import BrokerOrderStatus
from firmquant.persistence.database import Database
from firmquant.persistence.production_repository import MonotonicExecutionLedgerRepository
from firmquant.persistence.repositories import PersistenceConflict
from tests.fixtures.recovery_cases import broker_order, create_submitting_case


def test_broker_order_cumulative_fill_and_terminal_status_never_regress(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        case = create_submitting_case(database)
        repository = MonotonicExecutionLedgerRepository(database)
        acknowledged = broker_order(case.command, status=BrokerOrderStatus.ACKNOWLEDGED, sequence=20)
        partial = replace(
            acknowledged,
            status=BrokerOrderStatus.PARTIALLY_FILLED,
            filled_shares=case.command.requested_shares.__class__(50),
            event_sequence=5030,
        )
        regressed = replace(
            partial,
            status=BrokerOrderStatus.ACKNOWLEDGED,
            filled_shares=case.command.requested_shares.__class__(0),
            event_sequence=20,
        )

        with database.transaction():
            repository._record_broker_order(  # noqa: SLF001 - persistence invariant test
                acknowledged,
                execution_id=case.aggregate.intent.execution_id,
            )
            repository._record_broker_order(  # noqa: SLF001 - persistence invariant test
                partial,
                execution_id=case.aggregate.intent.execution_id,
            )

        with database.transaction(), pytest.raises(PersistenceConflict, match="regressed"):
            repository._record_broker_order(  # noqa: SLF001 - persistence invariant test
                regressed,
                execution_id=case.aggregate.intent.execution_id,
            )
    finally:
        database.close()
