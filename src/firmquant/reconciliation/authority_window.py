"""Authority-window rules for reconciling a today-only broker query with local history."""

from __future__ import annotations

from datetime import date

from firmquant.domain.orders import OrderState

_CROSS_SESSION_STATES = frozenset(
    {
        OrderState.SUBMITTING,
        OrderState.UNKNOWN,
        OrderState.ACKNOWLEDGED,
        OrderState.PARTIALLY_FILLED,
        OrderState.CANCEL_REQUESTED,
    }
)


def _calendar(value: date, *, label: str) -> date:
    if type(value) is not date:
        raise TypeError(f"{label} must be a calendar date")
    return value


def order_is_in_broker_authority_window(
    *,
    order_session: date,
    broker_session: date,
    local_state: OrderState,
) -> bool:
    """Keep today's orders plus prior-session nonterminal orders that still need proof."""

    order_point = _calendar(order_session, label="order session")
    broker_point = _calendar(broker_session, label="broker session")
    if not isinstance(local_state, OrderState):
        raise TypeError("local order state must be OrderState")
    return order_point == broker_point or local_state in _CROSS_SESSION_STATES


def fill_is_in_broker_authority_window(*, fill_session: date, broker_session: date) -> bool:
    """XtQuant normal trade queries are session-scoped; historical fills remain local evidence."""

    return _calendar(fill_session, label="fill session") == _calendar(
        broker_session,
        label="broker session",
    )


__all__ = (
    "fill_is_in_broker_authority_window",
    "order_is_in_broker_authority_window",
)
