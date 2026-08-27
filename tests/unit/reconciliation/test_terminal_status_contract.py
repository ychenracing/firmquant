from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from firmquant.domain.broker_facts import BrokerOrderStatus
from firmquant.domain.orders import OrderState
from firmquant.domain.values import Money, Shares
from firmquant.persistence.database import Database
from firmquant.reconciliation.models import ReconciliationKind
from firmquant.reconciliation.service import ReconciliationService
from tests.fixtures.reconciliation_cases import NOW, healthy_reconciliation_facts


@pytest.fixture
def database(tmp_path: Path):
    opened = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        yield opened
    finally:
        opened.close()


def _service(database: Database) -> ReconciliationService:
    return ReconciliationService(
        database=database,
        cash_tolerance=Money(Decimal("0.01")),
        clock=lambda: NOW,
    )


def _partial_terminal_facts(status: BrokerOrderStatus, local_state: OrderState):
    facts = healthy_reconciliation_facts()
    order = replace(
        facts.broker_snapshot.orders[0],
        status=status,
        filled_shares=Shares(40),
    )
    fill = replace(facts.broker_snapshot.fills[0], shares=Shares(40))
    position = replace(facts.broker_snapshot.positions[0], total_shares=Shares(40))
    broker_snapshot = replace(
        facts.broker_snapshot,
        account=replace(
            facts.broker_snapshot.account,
            available_cash=Money(Decimal("1594.9")),
            total_assets=Money(Decimal("1994.9")),
        ),
        positions=(position,),
        orders=(order,),
        fills=(fill,),
        raw_payload_sha256="7" * 64,
    )
    strategy_position = replace(
        facts.strategy_account.positions[0],
        total_shares=Shares(40),
    )
    strategy_account = replace(
        facts.strategy_account,
        available_cash=broker_snapshot.account.available_cash,
        total_assets=broker_snapshot.account.total_assets,
        positions=(strategy_position,),
    )
    operational_order = replace(
        facts.operational_ledger.orders[0],
        filled_shares=Shares(40),
        local_state=local_state,
    )
    operational = replace(facts.operational_ledger, orders=(operational_order,))
    return replace(
        facts,
        broker_snapshot=broker_snapshot,
        strategy_account=strategy_account,
        operational_ledger=operational,
    )


@pytest.mark.parametrize(
    ("broker_status", "local_state"),
    [
        (BrokerOrderStatus.CANCELLED, OrderState.CANCELLED),
        (BrokerOrderStatus.REJECTED, OrderState.REJECTED),
        (BrokerOrderStatus.EXPIRED, OrderState.EXPIRED),
    ],
)
def test_partial_terminal_broker_truth_reconciles_only_with_same_true_terminal(
    database: Database,
    broker_status: BrokerOrderStatus,
    local_state: OrderState,
) -> None:
    receipt = _service(database).run(
        ReconciliationKind.RECOVERY,
        _partial_terminal_facts(broker_status, local_state),
    )

    assert "ORDER_TERMINAL_STATE_MISMATCH" not in receipt.blockers
    assert "LOCAL_TERMINAL_BROKER_ACTIVE" not in receipt.blockers
    assert receipt.passed is True


def test_different_true_terminal_states_halt_instead_of_collapsing_to_cancelled(
    database: Database,
) -> None:
    facts = _partial_terminal_facts(BrokerOrderStatus.EXPIRED, OrderState.REJECTED)

    receipt = _service(database).run(ReconciliationKind.RECOVERY, facts)

    assert receipt.halt_required is True
    assert "ORDER_TERMINAL_STATE_MISMATCH" in receipt.blockers
