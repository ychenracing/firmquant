from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from firmquant.application.event_pump import DomainEventPump
from firmquant.broker.paper import PaperBroker, PaperCallbackDeliveryError
from firmquant.domain.broker_facts import BrokerOrderStatus, MarketSessionStatus
from firmquant.domain.orders import OrderState
from firmquant.execution.policy import ExecutionPolicy, FeeSchedule, FillModel
from firmquant.persistence.database import Database
from firmquant.persistence.recovery import RecoveryService
from firmquant.persistence.repositories import BrokerEventRepository
from tests.fixtures.broker_contract import gateway_facts, order_command, order_event
from tests.fixtures.recovery_cases import (
    NOW,
    broker_fill,
    broker_order,
    create_submitting_case,
    fake_recovery_broker,
)


def _paper() -> PaperBroker:
    facts = gateway_facts()
    return PaperBroker(
        account=facts.account,
        positions=(),
        instruments=(facts.instrument,),
        quotes=(facts.quote,),
        market_status=MarketSessionStatus.OPEN,
        policy=ExecutionPolicy(
            fill_model=FillModel(
                max_volume_participation=Decimal("1"),
                slippage_bps=Decimal(0),
            ),
            fee_schedule=FeeSchedule(
                commission_rate=Decimal("0.0003"),
                minimum_commission=Decimal("5"),
                stamp_duty_rate=Decimal("0.001"),
                transfer_fee_rate=Decimal("0.00001"),
                fee_quantum=Decimal("0.01"),
            ),
        ),
        clock=lambda: NOW,
    )


def test_duplicate_and_out_of_order_callbacks_are_serialized_idempotently(
    tmp_path: Path,
) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    pump = DomainEventPump(capacity=4, clock=lambda: NOW)
    late = order_event(event_id="event-late", sequence=22)
    early = order_event(event_id="event-early", sequence=21)
    try:
        pump.sink(late)
        pump.sink(early)
        pump.sink(early)
        writes: list[bool] = []

        def persist(envelope: object) -> None:
            from firmquant.broker.normalization import BrokerEventEnvelope

            assert isinstance(envelope, BrokerEventEnvelope)
            with database.transaction():
                writes.append(
                    BrokerEventRepository(database).append(
                        broker_event_id=envelope.broker_event_id,
                        event_type=envelope.event_type.value,
                        broker_sequence=envelope.broker_sequence,
                        session_date=envelope.session_date,
                        event_time=envelope.event_time,
                        received_at=envelope.received_at,
                        safe_payload=envelope.safe_payload,
                        raw_payload_sha256=envelope.raw_payload_sha256,
                    )
                )

        while pump.dispatch_one(persist):
            pass

        assert writes == [True, True, False]
        rows = database.query_all(
            "SELECT broker_event_id, broker_sequence FROM broker_events "
            "ORDER BY broker_sequence, broker_event_id"
        )
        assert [tuple(row) for row in rows] == [
            ("event-early", 21),
            ("event-late", 22),
        ]
        assert pump.halt_required is False
    finally:
        database.close()


def test_callback_writer_failure_retains_evidence_and_halts() -> None:
    pump = DomainEventPump(capacity=1, clock=lambda: NOW)
    pump.sink(order_event(event_id="event-writer-failure"))

    with pytest.raises(RuntimeError, match="injected writer failure"):
        pump.dispatch_one(lambda _envelope: (_ for _ in ()).throw(RuntimeError("injected writer failure")))

    assert pump.halt_required is True
    assert pump.halt_reason == "BROKER_EVENT_WRITER_FAILED"
    assert pump.failed_envelope is not None
    assert pump.failed_envelope.broker_event_id == "event-writer-failure"


def test_lost_paper_callback_blocks_new_writes_until_evidence_is_replayed() -> None:
    broker = _paper()
    broker.connect()

    def fail_once(_event: object) -> None:
        raise RuntimeError("injected callback loss")

    broker.subscribe(fail_once)
    with pytest.raises(PaperCallbackDeliveryError, match="reconcile"):
        broker.submit_order(order_command(identity="callback-loss"))

    assert len(broker.query_orders()) == 1
    with pytest.raises(PaperCallbackDeliveryError, match="reconcile"):
        broker.submit_order(order_command(identity="blocked-after-loss"))
    assert len(broker.query_orders()) == 1

    replayed: list[object] = []
    broker.subscribe(replayed.append)
    assert replayed
    recovered = broker.submit_order(order_command(identity="after-replay"))
    assert recovered.status is BrokerOrderStatus.FILLED


@pytest.mark.parametrize(
    ("broker_case", "expected_state", "unresolved"),
    [
        pytest.param("not-observed", OrderState.UNKNOWN, True, id="submit-timeout-not-observed"),
        pytest.param("accepted", OrderState.ACKNOWLEDGED, False, id="accepted-before-local-id"),
        pytest.param("partial", OrderState.PARTIALLY_FILLED, False, id="lost-partial-callback"),
    ],
)
def test_recovery_queries_broker_and_never_blindly_resubmits(
    broker_case: str,
    expected_state: OrderState,
    unresolved: bool,
    tmp_path: Path,
) -> None:
    database = Database.open(tmp_path / f"{broker_case}.sqlite3")
    try:
        case = create_submitting_case(database)
        if broker_case == "accepted":
            orders = (broker_order(case.command),)
            fills = ()
        elif broker_case == "partial":
            fill = broker_fill(case.command, shares=50)
            orders = (
                broker_order(
                    case.command,
                    status=BrokerOrderStatus.PARTIALLY_FILLED,
                    filled_shares=50,
                ),
            )
            fills = (fill,)
        else:
            orders = ()
            fills = ()
        broker = fake_recovery_broker(orders=orders, fills=fills)
        service = RecoveryService(
            database=database,
            account_store=None,
            account_path=None,
            gateway=broker,
            clock=lambda: NOW,
        )

        first = service.recover()
        second = service.recover()
        aggregate = case.repository.load(case.aggregate.intent.execution_id)

        assert aggregate is not None
        assert aggregate.state is expected_state
        assert (case.aggregate.intent.execution_id in first.unresolved_order_ids) is unresolved
        assert second.duplicate_orders == 0
        assert second.duplicate_fills == 0
        assert broker.submitted_commands == ()
        assert broker.cancelled_order_ids == ()
        if broker_case == "partial":
            assert aggregate.filled_shares.value == 50
            assert database.scalar("SELECT count(*) FROM fills") == 1
    finally:
        database.close()
