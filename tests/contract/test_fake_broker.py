from __future__ import annotations

from dataclasses import replace

import pytest

from firmquant.broker.fake import BrokerOperation, FakeBroker, ScriptedOutcome
from firmquant.broker.gateway import BrokerDisconnected
from firmquant.domain.broker_facts import BrokerOrderStatus
from tests.fixtures.broker_contract import (
    assert_read_gateway_contract,
    gateway_facts,
    order_command,
    order_event,
)


def _broker() -> FakeBroker:
    facts = gateway_facts()
    return FakeBroker(
        account=facts.account,
        positions=(),
        orders=(),
        fills=(),
        instruments=(facts.instrument,),
        quotes=(facts.quote,),
        market_status=facts.quote.market_status,
        clock=lambda: facts.quote.received_at,
    )


def test_fake_broker_passes_shared_read_contract() -> None:
    assert_read_gateway_contract(_broker())


def test_fake_broker_preserves_duplicate_and_out_of_order_callbacks() -> None:
    broker = _broker()
    broker.connect()
    received: list[dict[str, object]] = []
    broker.subscribe(received.append)
    late = order_event(event_id="event-late", sequence=22)
    early = order_event(event_id="event-early", sequence=21)
    response = gateway_facts().order
    broker.script(
        [
            ScriptedOutcome(
                operation=BrokerOperation.SUBMIT,
                response=response,
                callbacks=(late, early, early),
            )
        ]
    )

    assert broker.submit_order(order_command()) == response
    assert [event["event_id"] for event in received] == [
        "event-late",
        "event-early",
        "event-early",
    ]
    assert broker.submitted_commands == (order_command(),)
    assert broker.query_orders() == (response,)


def test_fake_broker_can_model_acceptance_followed_by_submit_timeout() -> None:
    broker = _broker()
    broker.connect()
    accepted = gateway_facts().order
    broker.script(
        [
            ScriptedOutcome(
                operation=BrokerOperation.SUBMIT,
                response=accepted,
                error=TimeoutError("broker response lost"),
            )
        ]
    )

    with pytest.raises(TimeoutError, match="response lost"):
        broker.submit_order(order_command())

    assert broker.submitted_commands == (order_command(),)
    assert broker.query_orders() == (accepted,)


def test_fake_broker_can_reject_then_disconnect_without_hiding_attempt() -> None:
    broker = _broker()
    broker.connect()
    rejected = replace(gateway_facts().order, status=BrokerOrderStatus.REJECTED)
    broker.script(
        [
            ScriptedOutcome(
                operation=BrokerOperation.SUBMIT,
                response=rejected,
                connected_after=False,
            )
        ]
    )

    assert broker.submit_order(order_command()) == rejected
    assert broker.submitted_commands == (order_command(),)
    assert broker.health().connected is False
    with pytest.raises(BrokerDisconnected):
        broker.query_orders()


def test_fake_cancel_is_scripted_and_auditable() -> None:
    facts = gateway_facts()
    broker = FakeBroker(
        account=facts.account,
        positions=(),
        orders=(facts.order,),
        fills=(),
        instruments=(facts.instrument,),
        quotes=(facts.quote,),
        market_status=facts.quote.market_status,
        clock=lambda: facts.quote.received_at,
    )
    broker.connect()
    cancelled = replace(facts.order, status=BrokerOrderStatus.CANCELLED)
    broker.script(
        [
            ScriptedOutcome(
                operation=BrokerOperation.CANCEL,
                response=cancelled,
            )
        ]
    )

    assert broker.cancel_order(facts.order.broker_order_id) == cancelled
    assert broker.cancelled_order_ids == (facts.order.broker_order_id,)
    assert broker.query_orders() == (cancelled,)
