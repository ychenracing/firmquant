from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from firmquant.domain.broker_facts import Side
from firmquant.domain.errors import DomainTransitionError
from firmquant.domain.events import (
    BrokerAcknowledged,
    BrokerRejected,
    CancelConfirmed,
    CancelRequested,
    FillReported,
    OrderArmed,
    OrderExpired,
    OrderValidated,
    SubmitOutcomeUnknown,
    SubmitStarted,
    UnknownResolvedNotAccepted,
)
from firmquant.domain.orders import ExecutionIntent, OrderAggregate, OrderState
from firmquant.domain.values import Price, Shares, Symbol


def _intent(*, requested_shares: int = 1_000) -> ExecutionIntent:
    return ExecutionIntent.create(
        decision_id="decision-2026-08-25",
        uquant_order_id="uquant-order-1",
        symbol=Symbol.parse("sz300308"),
        side=Side.BUY,
        requested_shares=Shares(requested_shares),
        strategy_session=date(2026, 8, 25),
        uquant_source_sha="1" * 40,
    )


def _submitting_order(*, requested_shares: int = 1_000) -> OrderAggregate:
    order = OrderAggregate.from_intent(_intent(requested_shares=requested_shares))
    order = order.apply(OrderValidated(event_id="evt-validate"))
    order = order.apply(OrderArmed(event_id="evt-arm"))
    return order.apply(SubmitStarted(event_id="evt-submit"))


def test_intent_identity_is_stable_and_binds_economic_fields() -> None:
    first = _intent()
    same = _intent()
    changed = _intent(requested_shares=900)

    assert first == same
    assert first.execution_id.startswith("exec_")
    assert first.idempotency_key != changed.idempotency_key
    assert first.execution_id != changed.execution_id


def test_submit_exception_becomes_unknown_not_rejected() -> None:
    order = _submitting_order()

    changed = order.apply(
        SubmitOutcomeUnknown(event_id="evt-timeout", diagnostic_code="BROKER_TIMEOUT")
    )

    assert changed.state is OrderState.UNKNOWN
    assert changed.submit_attempts == 1


def test_cancel_confirmation_can_resolve_before_ack_callback() -> None:
    submitting = _submitting_order()

    cancelled = submitting.apply(
        CancelConfirmed(event_id="evt-cancelled", broker_order_id="broker-order-1")
    )

    assert cancelled.state is OrderState.CANCELLED
    assert cancelled.broker_order_id == "broker-order-1"


def test_unknown_order_is_not_resubmitted_until_absence_is_proven() -> None:
    unknown = _submitting_order().apply(
        SubmitOutcomeUnknown(event_id="evt-timeout", diagnostic_code="BROKER_TIMEOUT")
    )

    with pytest.raises(DomainTransitionError, match=r"UNKNOWN.*SubmitStarted"):
        unknown.apply(SubmitStarted(event_id="evt-blind-resubmit"))
    resolved = unknown.apply(
        UnknownResolvedNotAccepted(event_id="evt-proof", evidence_sha256="a" * 64)
    )

    assert resolved.state is OrderState.ARMED
    assert resolved.apply(SubmitStarted(event_id="evt-retry")).submit_attempts == 2


def test_partial_fill_cancel_race_preserves_confirmed_fill() -> None:
    order = _submitting_order()
    order = order.apply(
        BrokerAcknowledged(event_id="evt-ack", broker_order_id="broker-order-1")
    )
    order = order.apply(
        FillReported(
            event_id="evt-fill-1",
            broker_fill_id="fill-1",
            broker_order_id="broker-order-1",
            shares=Shares(300),
            price=Price(Decimal("100.01")),
        )
    )
    order = order.apply(CancelRequested(event_id="evt-cancel-request"))
    raced = order.apply(
        FillReported(
            event_id="evt-fill-2",
            broker_fill_id="fill-2",
            broker_order_id="broker-order-1",
            shares=Shares(200),
            price=Price(Decimal("100.02")),
        )
    )

    assert raced.state is OrderState.CANCEL_REQUESTED
    cancelled = raced.apply(CancelConfirmed(event_id="evt-cancelled"))
    assert cancelled.state is OrderState.CANCELLED
    assert cancelled.filled_shares == Shares(500)


def test_late_fill_after_cancelled_is_retained_and_requires_investigation() -> None:
    cancelled = _submitting_order(requested_shares=1_000)
    cancelled = cancelled.apply(
        BrokerAcknowledged(event_id="evt-ack", broker_order_id="broker-order-1")
    )
    cancelled = cancelled.apply(CancelRequested(event_id="evt-cancel-request"))
    cancelled = cancelled.apply(CancelConfirmed(event_id="evt-cancelled"))

    changed = cancelled.apply(
        FillReported(
            event_id="evt-late-fill",
            broker_fill_id="fill-late",
            broker_order_id="broker-order-1",
            shares=Shares(100),
            price=Price(Decimal("99.90")),
        )
    )

    assert changed.state is OrderState.CANCELLED
    assert changed.filled_shares == Shares(100)
    assert changed.late_fill_investigation_required is True
    assert "LATE_FILL_AFTER_CANCELLED" in changed.anomalies


def test_duplicate_event_and_fill_are_idempotent_but_conflicts_are_rejected() -> None:
    order = _submitting_order().apply(
        BrokerAcknowledged(event_id="evt-ack", broker_order_id="broker-order-1")
    )
    fill = FillReported(
        event_id="evt-fill",
        broker_fill_id="fill-1",
        broker_order_id="broker-order-1",
        shares=Shares(100),
        price=Price(Decimal("100")),
    )
    changed = order.apply(fill)

    assert changed.apply(fill) is changed
    duplicate_callback = FillReported(
        event_id="evt-fill-duplicate-callback",
        broker_fill_id="fill-1",
        broker_order_id="broker-order-1",
        shares=Shares(100),
        price=Price(Decimal("100")),
    )
    assert changed.apply(duplicate_callback) is changed
    conflicting_callback = FillReported(
        event_id="evt-fill-conflict",
        broker_fill_id="fill-1",
        broker_order_id="broker-order-1",
        shares=Shares(200),
        price=Price(Decimal("100")),
    )
    with pytest.raises(DomainTransitionError, match="fill identity collision"):
        changed.apply(conflicting_callback)


def test_out_of_order_ack_does_not_regress_partial_fill() -> None:
    order = _submitting_order().apply(
        FillReported(
            event_id="evt-fill",
            broker_fill_id="fill-1",
            broker_order_id="broker-order-1",
            shares=Shares(100),
            price=Price(Decimal("100")),
        )
    )

    changed = order.apply(
        BrokerAcknowledged(event_id="evt-late-ack", broker_order_id="broker-order-1")
    )

    assert changed.state is OrderState.PARTIALLY_FILLED


def test_terminal_state_cannot_illegally_regress() -> None:
    expired = OrderAggregate.from_intent(_intent()).apply(
        OrderExpired(event_id="evt-expired", reason_code="WINDOW_CLOSED")
    )

    with pytest.raises(DomainTransitionError, match=r"EXPIRED.*OrderValidated"):
        expired.apply(OrderValidated(event_id="evt-validate"))


def test_rejected_order_is_terminal() -> None:
    rejected = _submitting_order().apply(
        BrokerRejected(event_id="evt-rejected", reason_code="BROKER_REJECTED")
    )

    assert rejected.state is OrderState.REJECTED
    with pytest.raises(DomainTransitionError):
        rejected.apply(CancelRequested(event_id="evt-cancel"))
