from __future__ import annotations

from datetime import date

from firmquant.domain.orders import OrderState
from firmquant.reconciliation.authority_window import (
    fill_is_in_broker_authority_window,
    order_is_in_broker_authority_window,
)


def test_prior_session_terminal_order_is_not_required_from_today_only_broker_query() -> None:
    previous = date(2026, 8, 24)
    today = date(2026, 8, 25)

    assert (
        order_is_in_broker_authority_window(
            order_session=previous,
            broker_session=today,
            local_state=OrderState.FILLED,
        )
        is False
    )
    assert fill_is_in_broker_authority_window(fill_session=previous, broker_session=today) is False


def test_prior_session_unresolved_order_remains_in_authority_window_until_explained() -> None:
    previous = date(2026, 8, 24)
    today = date(2026, 8, 25)

    for state in (
        OrderState.SUBMITTING,
        OrderState.UNKNOWN,
        OrderState.ACKNOWLEDGED,
        OrderState.PARTIALLY_FILLED,
        OrderState.CANCEL_REQUESTED,
    ):
        assert order_is_in_broker_authority_window(
            order_session=previous,
            broker_session=today,
            local_state=state,
        )


def test_current_session_orders_and_fills_are_always_reconciled() -> None:
    today = date(2026, 8, 25)

    assert order_is_in_broker_authority_window(
        order_session=today,
        broker_session=today,
        local_state=OrderState.FILLED,
    )
    assert fill_is_in_broker_authority_window(fill_session=today, broker_session=today)
