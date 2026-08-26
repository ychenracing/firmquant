from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from firmquant.domain.broker_facts import BrokerPositionFact, Side
from firmquant.domain.events import (
    BrokerAcknowledged,
    FillReported,
    OrderArmed,
    OrderValidated,
    SubmitStarted,
)
from firmquant.domain.orders import (
    ORDER_TRANSITIONS,
    TERMINAL_ORDER_STATES,
    ExecutionIntent,
    OrderAggregate,
)
from firmquant.domain.values import Money, Price, Shares, Symbol
from firmquant.risk.gate import ExecutionRiskGate
from tests.fixtures.risk_cases import SYMBOL, quote, risk_command, risk_context


def _acknowledged(requested_shares: int) -> OrderAggregate:
    intent = ExecutionIntent.create(
        decision_id="decision-economic-property",
        uquant_order_id="O-ECONOMIC-PROPERTY",
        symbol=SYMBOL,
        side=Side.BUY,
        requested_shares=Shares(requested_shares),
        strategy_session=date(2026, 8, 25),
        uquant_source_sha="1" * 40,
    )
    aggregate = OrderAggregate.from_intent(intent)
    aggregate = aggregate.apply(OrderValidated(event_id="validated"))
    aggregate = aggregate.apply(OrderArmed(event_id="armed"))
    aggregate = aggregate.apply(SubmitStarted(event_id="submitting"))
    return aggregate.apply(BrokerAcknowledged(event_id="acknowledged", broker_order_id="broker-property"))


@settings(max_examples=40, deadline=None)
@given(chunks=st.lists(st.integers(min_value=1, max_value=2_000), min_size=1, max_size=8))
def test_duplicate_fill_stream_never_changes_final_economics(chunks: list[int]) -> None:
    requested = sum(chunks)
    events = tuple(
        FillReported(
            event_id=f"fill-event-{index}",
            broker_fill_id=f"fill-{index}",
            broker_order_id="broker-property",
            shares=Shares(shares),
            price=Price(Decimal("10")),
        )
        for index, shares in enumerate(chunks)
    )
    once = _acknowledged(requested)
    duplicated = _acknowledged(requested)
    for event in events:
        once = once.apply(event)
        duplicated = duplicated.apply(event).apply(event)

    assert duplicated == once
    assert once.filled_shares == Shares(requested)
    assert once.filled_shares.value <= once.intent.requested_shares.value


@settings(max_examples=60, deadline=None)
@given(
    requested=st.integers(min_value=1, max_value=5_000),
    authorized=st.integers(min_value=1, max_value=5_000),
    available_cash=st.integers(min_value=0, max_value=100_000),
    volume=st.integers(min_value=0, max_value=2_000_000),
)
def test_buy_authorization_never_produces_negative_cash(
    requested: int,
    authorized: int,
    available_cash: int,
    volume: int,
) -> None:
    context = replace(
        risk_context(),
        available_cash=Money(Decimal(available_cash)),
        quote=quote(volume=volume),
    )
    command = risk_command(requested=requested, authorized=authorized)

    decision = ExecutionRiskGate().evaluate(command, context)

    shares = decision.authorized_shares.value
    committed_cash = Decimal(shares) * command.command.limit_price.value
    if shares:
        committed_cash += command.estimated_fees.value
    assert committed_cash <= context.available_cash.value
    assert shares <= requested
    assert shares <= authorized


@settings(max_examples=60, deadline=None)
@given(
    requested=st.integers(min_value=1, max_value=10_000),
    authorized=st.integers(min_value=1, max_value=10_000),
    total=st.integers(min_value=0, max_value=10_000),
    sellable=st.integers(min_value=0, max_value=10_000),
)
def test_sell_authorization_can_never_create_a_negative_position(
    requested: int,
    authorized: int,
    total: int,
    sellable: int,
) -> None:
    sellable = min(total, sellable)
    position = BrokerPositionFact(
        symbol=SYMBOL,
        total_shares=Shares(total),
        sellable_shares=Shares(sellable),
        average_cost=None if total == 0 else Price(Decimal("9")),
        market_value=Money(Decimal(total * 10)),
    )
    context = replace(
        risk_context(),
        position=position,
        uquant_target_shares=Shares(0),
        uquant_target_weight=Decimal(0),
        actual_symbol_notional=position.market_value,
        actual_gross_notional=position.market_value,
    )

    decision = ExecutionRiskGate().evaluate(
        risk_command(side=Side.SELL, requested=requested, authorized=authorized),
        context,
    )

    assert decision.authorized_shares.value <= total
    assert decision.authorized_shares.value <= sellable
    assert total - decision.authorized_shares.value >= 0


@given(raw_symbol=st.sampled_from(("600519.SH", "000001.SZ", "830799.BJ")))
def test_non_ai_universe_symbol_never_receives_submit_authority(raw_symbol: str) -> None:
    foreign = Symbol.parse(raw_symbol)
    base = risk_command()
    command = replace(base, command=replace(base.command, symbol=foreign))
    base_context = risk_context()
    assert base_context.instrument is not None
    assert base_context.quote is not None
    context = replace(
        base_context,
        instrument=replace(base_context.instrument, symbol=foreign),
        quote=replace(base_context.quote, symbol=foreign),
    )

    decision = ExecutionRiskGate().evaluate(command, context)

    assert decision.authorized_shares == Shares(0)
    assert "SYMBOL_NOT_CANONICAL_AI_UNIVERSE" in decision.reason_codes


def test_every_terminal_order_state_has_no_legal_regression() -> None:
    assert TERMINAL_ORDER_STATES
    assert all(ORDER_TRANSITIONS[state] == frozenset() for state in TERMINAL_ORDER_STATES)
