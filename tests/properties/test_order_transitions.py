from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from firmquant.domain.broker_facts import Side
from firmquant.domain.errors import DomainTransitionError
from firmquant.domain.events import (
    BrokerAcknowledged,
    FillReported,
    OrderArmed,
    OrderValidated,
    SubmitStarted,
)
from firmquant.domain.orders import ExecutionIntent, OrderAggregate
from firmquant.domain.values import Price, Shares, Symbol


def _acknowledged(requested_shares: int) -> OrderAggregate:
    intent = ExecutionIntent.create(
        decision_id="decision",
        uquant_order_id="order",
        symbol=Symbol.parse("sz300308"),
        side=Side.BUY,
        requested_shares=Shares(requested_shares),
        strategy_session=date(2026, 8, 25),
        uquant_source_sha="1" * 40,
    )
    order = OrderAggregate.from_intent(intent)
    order = order.apply(OrderValidated(event_id="validate"))
    order = order.apply(OrderArmed(event_id="arm"))
    order = order.apply(SubmitStarted(event_id="submit"))
    return order.apply(BrokerAcknowledged(event_id="ack", broker_order_id="broker-1"))


@given(
    requested=st.integers(min_value=1, max_value=1_000_000),
    first=st.integers(min_value=1, max_value=1_000_000),
)
def test_filled_shares_never_exceed_requested(requested: int, first: int) -> None:
    order = _acknowledged(requested)
    event = FillReported(
        event_id="fill",
        broker_fill_id="fill-1",
        broker_order_id="broker-1",
        shares=Shares(first),
        price=Price(Decimal("1")),
    )

    if first > requested:
        with pytest.raises(DomainTransitionError, match="exceed requested"):
            order.apply(event)
    else:
        assert order.apply(event).filled_shares.value == first


@given(st.integers(min_value=1, max_value=1_000_000))
def test_duplicate_event_never_changes_aggregate(shares: int) -> None:
    order = _acknowledged(shares)
    event = FillReported(
        event_id="fill",
        broker_fill_id="fill-1",
        broker_order_id="broker-1",
        shares=Shares(shares),
        price=Price(Decimal("1")),
    )
    filled = order.apply(event)

    assert filled.apply(event) is filled
