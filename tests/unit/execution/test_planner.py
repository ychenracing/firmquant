from __future__ import annotations

from dataclasses import replace

from firmquant.broker.normalization import normalize_order
from firmquant.domain.broker_facts import BrokerOrderStatus, PriceType, Side
from firmquant.domain.values import Shares
from firmquant.execution.planner import ExecutionPlanner
from tests.fixtures.session_cases import (
    BUY_SYMBOL,
    EXECUTION_SESSION,
    NOW,
    decision_snapshot,
    execution_snapshot,
)


def test_plan_is_stable_sell_first_and_never_expands_target_gap() -> None:
    decision = decision_snapshot()
    facts = execution_snapshot()

    first = ExecutionPlanner().plan(decision, facts)
    second = ExecutionPlanner().plan(decision, facts)
    sell, buy = first.orders

    assert first.plan_id == second.plan_id
    assert sell.side is Side.SELL
    assert sell.uquant_authorized_shares == Shares(1000)
    assert sell.uquant_authorized_shares.value == sell.current_shares.value
    assert buy.side is Side.BUY
    assert buy.uquant_authorized_shares == Shares(800)
    assert buy.uquant_authorized_shares.value == (buy.target_shares.value - buy.current_shares.value)


def test_sentinel_freeze_blocks_only_new_buy_intent() -> None:
    plan = ExecutionPlanner().plan(
        decision_snapshot(freeze_new_risk=True),
        execution_snapshot(),
    )

    assert [item.side for item in plan.orders] == [Side.SELL]
    assert [(item.uquant_order_id, item.reason_code) for item in plan.blockers] == [
        ("O-BUY-1", "SENTINEL_FREEZE_NEW_RISK")
    ]


def test_existing_broker_order_prevents_duplicate_economic_order() -> None:
    facts = execution_snapshot()
    raw = {
        "broker_order_id": "already-submitted",
        "client_order_id": "O-BUY-1",
        "symbol": BUY_SYMBOL.canonical,
        "side": Side.BUY.value,
        "price_type": PriceType.LIMIT.value,
        "status": BrokerOrderStatus.ACKNOWLEDGED.value,
        "requested_shares": 100,
        "filled_shares": 0,
        "limit_price": "10.00",
        "session_date": EXECUTION_SESSION.isoformat(),
        "event_time": NOW.isoformat(),
        "event_sequence": 11,
    }
    existing = normalize_order(raw, received_at=NOW)
    blocked_facts = replace(
        facts,
        broker_snapshot=replace(facts.broker_snapshot, orders=(existing,)),
    )

    plan = ExecutionPlanner().plan(
        decision_snapshot(include_sell=False, include_buy=True),
        blocked_facts,
    )

    assert plan.orders == ()
    assert [(item.uquant_order_id, item.reason_code) for item in plan.blockers] == [
        ("O-BUY-1", "EXISTING_BROKER_ORDER")
    ]
