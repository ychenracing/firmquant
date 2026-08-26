from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from firmquant.domain.broker_facts import AccountType, BrokerOrderStatus
from firmquant.persistence.database import Database
from firmquant.reconciliation.live_view import build_operational_ledger_view
from tests.fixtures.recovery_cases import (
    broker_fill,
    broker_order,
    create_submitting_case,
)


def test_live_view_keeps_unresolved_submit_even_before_broker_id_is_known(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        case = create_submitting_case(database)
        view = build_operational_ledger_view(
            database,
            broker_session=case.command.strategy_session,
            expected_account_id_hash="a" * 64,
            expected_account_type=AccountType.CASH,
        )
        assert view.orders == ()
        assert view.submitting_unresolved_execution_ids == (case.aggregate.intent.execution_id,)
    finally:
        database.close()


def test_live_view_does_not_require_prior_terminal_order_or_fill_from_today_only_query(
    tmp_path: Path,
) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        case = create_submitting_case(database)
        fact = broker_order(
            case.command,
            status=BrokerOrderStatus.FILLED,
            filled_shares=100,
            sequence=20,
        )
        fill = broker_fill(case.command, shares=100, sequence=21)
        with database.transaction():
            case.repository.record_submit_result(
                case.aggregate,
                case.attempt,
                fact,
                (fill,),
                received_at=fill.received_at,
            )
        next_session = case.command.strategy_session + timedelta(days=1)
        view = build_operational_ledger_view(
            database,
            broker_session=next_session,
            expected_account_id_hash="a" * 64,
            expected_account_type=AccountType.CASH,
        )
        assert view.orders == ()
        assert view.known_broker_fill_ids == frozenset()
        assert view.unresolved_execution_ids == ()
    finally:
        database.close()
