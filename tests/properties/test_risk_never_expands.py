from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from firmquant.domain.broker_facts import Side
from firmquant.risk.gate import ExecutionRiskGate
from tests.fixtures.risk_cases import quote, risk_command, risk_context


@given(
    requested=st.integers(min_value=1, max_value=10_000),
    authorized=st.integers(min_value=1, max_value=10_000),
    cash=st.integers(min_value=0, max_value=1_000_000),
    volume=st.integers(min_value=0, max_value=10_000_000),
)
def test_gate_never_expands_uquant_or_requested_quantity(
    requested: int, authorized: int, cash: int, volume: int
) -> None:
    command = risk_command(requested=requested, authorized=authorized)
    context = replace(
        risk_context(),
        available_cash=replace(risk_context().available_cash, value=Decimal(cash)),
        quote=quote(volume=volume),
    )

    decision = ExecutionRiskGate().evaluate(command, context)

    assert decision.authorized_shares.value <= requested
    assert decision.authorized_shares.value <= authorized


@given(
    requested=st.integers(min_value=1, max_value=10_000),
    total=st.integers(min_value=0, max_value=10_000),
    sellable=st.integers(min_value=0, max_value=10_000),
)
def test_sell_authorization_never_exceeds_position_or_sellable(
    requested: int, total: int, sellable: int
) -> None:
    from firmquant.domain.broker_facts import BrokerPositionFact
    from firmquant.domain.values import Money, Price, Shares

    sellable = min(total, sellable)
    position = BrokerPositionFact(
        symbol=risk_context().instrument.symbol,  # type: ignore[union-attr]
        total_shares=Shares(total),
        sellable_shares=Shares(sellable),
        average_cost=None if total == 0 else Price(Decimal("9")),
        market_value=Money(Decimal(total * 10)),
    )
    authorized = max(1, requested)
    context = replace(
        risk_context(),
        position=position,
        uquant_target_shares=Shares(0),
        uquant_target_weight=Decimal(0),
        actual_symbol_notional=position.market_value,
        actual_gross_notional=position.market_value,
    )

    decision = ExecutionRiskGate().evaluate(
        risk_command(
            side=Side.SELL,
            requested=requested,
            authorized=authorized,
        ),
        context,
    )

    assert decision.authorized_shares.value <= requested
    assert decision.authorized_shares.value <= total
    assert decision.authorized_shares.value <= sellable
