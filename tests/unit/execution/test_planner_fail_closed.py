from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from firmquant.domain.broker_facts import (
    MarketSessionStatus,
    SecurityStatus,
    SecurityType,
)
from firmquant.domain.errors import DomainTypeError, DomainValidationError
from firmquant.domain.values import Price, Shares
from firmquant.execution.planner import (
    ExecutionBrokerSnapshot,
    ExecutionPlanner,
    ExecutionPlanningError,
    PlannedOrder,
    _text,
    _weight,
)
from tests.fixtures.broker_contract import gateway_facts
from tests.fixtures.session_cases import decision_snapshot, execution_snapshot


def _snapshot_payload(**changes: object):  # type: ignore[no-untyped-def]
    snapshot = decision_snapshot()
    payload = json.loads(snapshot.payload_json)
    payload.update(changes)
    object.__setattr__(snapshot, "payload_json", json.dumps(payload, allow_nan=True))
    return snapshot


def _pending_change(**changes: object):  # type: ignore[no-untyped-def]
    snapshot = decision_snapshot(include_sell=False, include_buy=True)
    payload = json.loads(snapshot.payload_json)
    pending = payload["pending_orders"]
    assert isinstance(pending, list) and isinstance(pending[0], dict)
    pending[0].update(changes)
    object.__setattr__(snapshot, "payload_json", json.dumps(payload, allow_nan=True))
    return snapshot


@pytest.mark.parametrize(
    ("factory", "exception"),
    [
        (lambda: _text(None, label="value"), ExecutionPlanningError),
        (lambda: _text("", label="value"), ExecutionPlanningError),
        (lambda: _text(" bad", label="value"), ExecutionPlanningError),
        (lambda: _weight(True), ExecutionPlanningError),
        (lambda: _weight("0.5"), ExecutionPlanningError),
        (lambda: _weight(Decimal("NaN")), ExecutionPlanningError),
        (lambda: _weight(Decimal("-0.1")), ExecutionPlanningError),
        (lambda: _weight(Decimal("1.1")), ExecutionPlanningError),
    ],
)
def test_planner_primitive_contract_rejects_noncanonical_values(
    factory: Callable[[], object], exception: type[Exception]
) -> None:
    with pytest.raises(exception):
        factory()


def test_planner_weight_accepts_only_exact_json_number_domain() -> None:
    assert _weight(0) == Decimal(0)
    assert _weight(1) == Decimal(1)
    assert _weight(Decimal("0.5")) == Decimal("0.5")


@pytest.mark.parametrize(
    "change",
    [
        {"broker_snapshot": object()},
        {"instruments": []},
        {"quotes": []},
        {"market_status": "OPEN"},
        {"instruments": execution_snapshot().instruments * 2},
        {"quotes": execution_snapshot().quotes * 2},
        {
            "instruments": (
                replace(execution_snapshot().instruments[0], session_date=date(2026, 8, 26)),
                execution_snapshot().instruments[1],
            )
        },
        {
            "quotes": (
                replace(execution_snapshot().quotes[0], session_date=date(2026, 8, 26)),
                execution_snapshot().quotes[1],
            )
        },
        {
            "quotes": tuple(
                replace(quote, market_status=MarketSessionStatus.CLOSED)
                for quote in execution_snapshot().quotes
            )
        },
    ],
)
def test_execution_snapshot_rejects_mixed_or_duplicate_broker_facts(change: dict[str, object]) -> None:
    with pytest.raises((DomainTypeError, DomainValidationError)):
        replace(execution_snapshot(), **change)


def test_execution_snapshot_hash_is_order_independent() -> None:
    facts = execution_snapshot()
    reversed_facts = replace(
        facts,
        instruments=tuple(reversed(facts.instruments)),
        quotes=tuple(reversed(facts.quotes)),
    )
    assert reversed_facts.sha256 == facts.sha256


@pytest.mark.parametrize(
    "snapshot",
    [
        _snapshot_payload(pending_orders=None),
        _snapshot_payload(sentinel=None),
        _snapshot_payload(sentinel={"freeze_new_risk": "false"}),
        _snapshot_payload(pending_orders=["not-an-object"]),
        _pending_change(order_id=""),
        _pending_change(symbol=""),
        _pending_change(symbol="INVALID"),
        _pending_change(side="HOLD"),
        _pending_change(target_weight=True),
        _pending_change(target_weight="0.8"),
        _pending_change(target_weight=2),
        _pending_change(reason_code=""),
    ],
)
def test_planner_rejects_corrupt_frozen_decision_payload(snapshot: object) -> None:
    with pytest.raises(ExecutionPlanningError):
        ExecutionPlanner().plan(snapshot, execution_snapshot())  # type: ignore[arg-type]


def test_planner_rejects_unparseable_non_object_and_nonstandard_json() -> None:
    for raw in ("{", "[]", '{"pending_orders":NaN,"sentinel":{}}'):
        snapshot = decision_snapshot()
        object.__setattr__(snapshot, "payload_json", raw)
        with pytest.raises(ExecutionPlanningError):
            ExecutionPlanner().plan(snapshot, execution_snapshot())


def test_planner_rejects_untyped_inputs_and_nonfuture_session() -> None:
    planner = ExecutionPlanner()
    with pytest.raises(DomainTypeError):
        planner.plan(object(), execution_snapshot())  # type: ignore[arg-type]
    with pytest.raises(DomainTypeError):
        planner.plan(decision_snapshot(), object())  # type: ignore[arg-type]

    facts = execution_snapshot()
    same_session = replace(
        facts,
        broker_snapshot=replace(
            facts.broker_snapshot,
            session_date=decision_snapshot().strategy_session,
        ),
        instruments=tuple(
            replace(item, session_date=decision_snapshot().strategy_session) for item in facts.instruments
        ),
        quotes=tuple(
            replace(item, session_date=decision_snapshot().strategy_session) for item in facts.quotes
        ),
    )
    with pytest.raises(ExecutionPlanningError, match="must follow"):
        planner.plan(decision_snapshot(), same_session)


def _reason_codes(plan) -> set[str]:  # type: ignore[no-untyped-def]
    return {blocker.reason_code for blocker in plan.blockers}


def test_planner_records_existing_broker_order_and_sentinel_freeze() -> None:
    facts = execution_snapshot()
    existing = replace(gateway_facts().order, client_order_id="O-BUY-1")
    with_existing = replace(
        facts,
        broker_snapshot=replace(facts.broker_snapshot, orders=(existing,)),
    )
    plan = ExecutionPlanner().plan(
        decision_snapshot(include_sell=False, include_buy=True),
        with_existing,
    )
    assert _reason_codes(plan) == {"EXISTING_BROKER_ORDER"}

    frozen = ExecutionPlanner().plan(
        decision_snapshot(include_sell=False, include_buy=True, freeze_new_risk=True),
        facts,
    )
    assert _reason_codes(frozen) == {"SENTINEL_FREEZE_NEW_RISK"}


@pytest.mark.parametrize(
    ("facts", "reason"),
    [
        (replace(execution_snapshot(), instruments=()), "INSTRUMENT_FACT_MISSING"),
        (replace(execution_snapshot(), quotes=()), "QUOTE_FACT_MISSING"),
        (
            replace(
                execution_snapshot(),
                instruments=tuple(
                    replace(item, security_type=SecurityType.FUND)
                    for item in execution_snapshot().instruments
                ),
            ),
            "INSTRUMENT_NOT_TRADING",
        ),
        (
            replace(
                execution_snapshot(),
                instruments=tuple(
                    replace(item, status=SecurityStatus.SUSPENDED)
                    for item in execution_snapshot().instruments
                ),
            ),
            "INSTRUMENT_NOT_TRADING",
        ),
        (
            replace(
                execution_snapshot(),
                market_status=MarketSessionStatus.CLOSED,
                quotes=tuple(
                    replace(item, market_status=MarketSessionStatus.CLOSED)
                    for item in execution_snapshot().quotes
                ),
            ),
            "MARKET_NOT_TRADABLE",
        ),
        (
            replace(
                execution_snapshot(),
                quotes=tuple(replace(item, ask_price=None) for item in execution_snapshot().quotes),
            ),
            "REFERENCE_PRICE_MISSING",
        ),
        (
            replace(
                execution_snapshot(),
                quotes=tuple(replace(item, lower_limit=None) for item in execution_snapshot().quotes),
            ),
            "PRICE_LIMIT_FACT_MISSING",
        ),
    ],
)
def test_planner_turns_missing_execution_facts_into_deterministic_blockers(
    facts: ExecutionBrokerSnapshot, reason: str
) -> None:
    snapshot = decision_snapshot(include_sell=False, include_buy=True)
    plan = ExecutionPlanner().plan(snapshot, facts)
    assert _reason_codes(plan) == {reason}
    assert plan.orders == ()


def test_target_already_satisfied_is_a_blocker_not_a_reversed_order() -> None:
    plan = ExecutionPlanner().plan(
        _pending_change(target_weight=0),
        execution_snapshot(),
    )
    assert _reason_codes(plan) == {"TARGET_ALREADY_SATISFIED"}
    assert plan.orders == ()


def test_planned_order_cannot_expand_the_target_gap() -> None:
    valid = (
        ExecutionPlanner()
        .plan(
            decision_snapshot(include_sell=False, include_buy=True),
            execution_snapshot(),
        )
        .orders[0]
    )
    with pytest.raises(DomainValidationError, match="positive"):
        replace(valid, uquant_authorized_shares=Shares(0))
    with pytest.raises(DomainValidationError, match="expands target gap"):
        replace(valid, uquant_authorized_shares=Shares(10_000))

    liquidation = PlannedOrder(
        decision_id=valid.decision_id,
        uquant_order_id=valid.uquant_order_id,
        symbol=valid.symbol,
        side=valid.side,
        target_weight=Decimal(0),
        uquant_authorized_shares=Shares(100),
        current_shares=Shares(100),
        target_shares=Shares(0),
        trading_unit=Shares(100),
        limit_price=Price(Decimal("10")),
        strategy_session=valid.strategy_session,
        execution_session=valid.execution_session,
        uquant_source_sha=valid.uquant_source_sha,
        reason_code="RISK_REDUCTION",
    )
    assert liquidation.uquant_authorized_shares == Shares(100)
