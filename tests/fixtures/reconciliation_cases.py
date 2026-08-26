from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from firmquant.domain.orders import OrderState
from firmquant.domain.values import Money
from firmquant.reconciliation.models import (
    ExpectedPosition,
    OperationalLedgerView,
    OperationalOrderView,
    ReconciliationFacts,
    StrategyAccountView,
)
from tests.fixtures.broker_snapshots import completed_buy_snapshot

NOW = datetime(2026, 1, 6, 8, 5, tzinfo=UTC)


def healthy_reconciliation_facts() -> ReconciliationFacts:
    snapshot = completed_buy_snapshot()
    position = snapshot.positions[0]
    order = snapshot.orders[0]
    strategy = StrategyAccountView(
        available_cash=snapshot.account.available_cash,
        total_assets=snapshot.account.total_assets,
        positions=(
            ExpectedPosition(
                symbol=position.symbol,
                total_shares=position.total_shares,
                sellable_shares=position.sellable_shares,
            ),
        ),
        known_uquant_order_ids=frozenset({"O000000001"}),
        economic_state_sha256="e" * 64,
    )
    operational = OperationalLedgerView(
        expected_account_id_hash=snapshot.account.account_id_hash,
        expected_account_type=snapshot.account.account_type,
        orders=(
            OperationalOrderView(
                broker_order_id=order.broker_order_id,
                uquant_order_id="O000000001",
                symbol=order.symbol,
                side=order.side,
                requested_shares=order.requested_shares,
                filled_shares=order.filled_shares,
                local_state=OrderState.FILLED,
            ),
        ),
        known_broker_fill_ids=frozenset({"broker-fill-1"}),
        unresolved_execution_ids=(),
        submitting_unresolved_execution_ids=(),
    )
    return ReconciliationFacts(
        broker_snapshot=snapshot,
        strategy_account=strategy,
        operational_ledger=operational,
        company_action_suspected_symbols=frozenset(),
        uquant_code_identity_matches=True,
        data_identity_matches=True,
        config_identity_matches=True,
    )


def with_strategy_cash(value: str) -> ReconciliationFacts:
    facts = healthy_reconciliation_facts()
    return replace(
        facts,
        strategy_account=replace(
            facts.strategy_account,
            available_cash=Money(Decimal(value)),
        ),
    )
