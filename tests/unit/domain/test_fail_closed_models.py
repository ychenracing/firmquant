from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from firmquant.domain.broker_facts import (
    AccountType,
    BrokerAccountFact,
    BrokerFillFact,
    BrokerPositionFact,
    FillStatus,
    Side,
)
from firmquant.domain.errors import DomainTypeError, DomainValidationError
from firmquant.domain.events import (
    BrokerAcknowledged,
    BrokerRejected,
    CancelConfirmed,
    FillReported,
    OrderEvent,
    OrderExpired,
    SubmitOutcomeUnknown,
    UnknownResolvedNotAccepted,
    event_fingerprint,
)
from firmquant.domain.orders import (
    AppliedEventIdentity,
    AppliedFill,
    ExecutionIntent,
    OrderAggregate,
    OrderState,
)
from firmquant.domain.values import Money, Price, Shares, Symbol
from tests.fixtures.broker_contract import gateway_facts
from tests.fixtures.session_cases import execution_snapshot

NOW = datetime(2026, 8, 25, 1, 31, tzinfo=UTC)
SESSION = date(2026, 8, 25)


def _fill() -> BrokerFillFact:
    return BrokerFillFact(
        broker_fill_id="fill-1",
        broker_order_id="broker-order-1",
        symbol=Symbol.parse("600519.SH"),
        side=Side.BUY,
        status=FillStatus.CONFIRMED,
        shares=Shares(100),
        price=Price(Decimal("10.10")),
        commission=Money(Decimal("1")),
        stamp_duty=Money(Decimal("0")),
        transfer_fee=Money(Decimal("0.01")),
        session_date=SESSION,
        event_time=NOW,
        received_at=NOW,
        event_sequence=1,
        raw_payload_sha256="f" * 64,
    )


def _intent() -> ExecutionIntent:
    return ExecutionIntent.create(
        decision_id="decision-1",
        uquant_order_id="order-1",
        symbol=Symbol.parse("600519.SH"),
        side=Side.BUY,
        requested_shares=Shares(100),
        strategy_session=SESSION,
        uquant_source_sha="1" * 40,
    )


@pytest.mark.parametrize(
    ("factory", "exception"),
    [
        (lambda: replace(gateway_facts().account, account_id_hash="bad"), DomainValidationError),
        (lambda: replace(gateway_facts().account, account_type="CASH"), DomainTypeError),
        (
            lambda: BrokerAccountFact(
                account_id_hash="a" * 64,
                account_type=AccountType.CASH,
                available_cash=Money(Decimal("2")),
                total_assets=Money(Decimal("1")),
            ),
            DomainValidationError,
        ),
        (
            lambda: BrokerPositionFact(
                symbol="600519.SH",  # type: ignore[arg-type]
                total_shares=Shares(1),
                sellable_shares=Shares(0),
                average_cost=None,
                market_value=Money(Decimal(0)),
            ),
            DomainTypeError,
        ),
        (
            lambda: BrokerPositionFact(
                symbol=Symbol.parse("600519.SH"),
                total_shares=Shares(1),
                sellable_shares=Shares(2),
                average_cost=None,
                market_value=Money(Decimal(0)),
            ),
            DomainValidationError,
        ),
        (lambda: replace(gateway_facts().instrument, symbol="bad"), DomainTypeError),
        (lambda: replace(gateway_facts().instrument, security_type="EQUITY"), DomainTypeError),
        (lambda: replace(gateway_facts().instrument, status="TRADING"), DomainTypeError),
        (lambda: replace(gateway_facts().instrument, trading_unit=Shares(0)), DomainValidationError),
        (lambda: replace(gateway_facts().instrument, price_precision=True), DomainTypeError),
        (lambda: replace(gateway_facts().instrument, price_precision=9), DomainValidationError),
        (
            lambda: replace(
                gateway_facts().instrument,
                price_tick=Price(Decimal("0.001")),
            ),
            DomainValidationError,
        ),
        (
            lambda: replace(
                gateway_facts().instrument,
                lower_limit=Price(Decimal("11")),
                upper_limit=Price(Decimal("10")),
            ),
            DomainValidationError,
        ),
        (lambda: replace(gateway_facts().instrument, session_date=NOW), DomainTypeError),
        (
            lambda: replace(gateway_facts().instrument, observed_at=datetime(2026, 8, 25)),
            DomainValidationError,
        ),
        (lambda: replace(gateway_facts().quote, symbol="bad"), DomainTypeError),
        (lambda: replace(gateway_facts().quote, market_status="OPEN"), DomainTypeError),
        (lambda: replace(gateway_facts().quote, sequence=True), DomainTypeError),
        (lambda: replace(gateway_facts().quote, sequence=-1), DomainValidationError),
        (lambda: replace(gateway_facts().quote, session_date=NOW), DomainTypeError),
        (
            lambda: replace(gateway_facts().quote, event_time=datetime(2026, 8, 25)),
            DomainValidationError,
        ),
        (
            lambda: replace(gateway_facts().quote, received_at=datetime(2026, 8, 25)),
            DomainValidationError,
        ),
        (
            lambda: replace(
                gateway_facts().quote,
                lower_limit=Price(Decimal("11")),
                upper_limit=Price(Decimal("10")),
            ),
            DomainValidationError,
        ),
        (
            lambda: replace(
                gateway_facts().quote,
                bid_price=Price(Decimal("10.20")),
                ask_price=Price(Decimal("10.10")),
            ),
            DomainValidationError,
        ),
        (
            lambda: replace(gateway_facts().quote, last_price=Price(Decimal("8.99"))),
            DomainValidationError,
        ),
        (
            lambda: replace(gateway_facts().quote, ask_price=Price(Decimal("11.01"))),
            DomainValidationError,
        ),
        (lambda: replace(gateway_facts().order, broker_order_id=""), DomainValidationError),
        (lambda: replace(gateway_facts().order, client_order_id=" bad"), DomainValidationError),
        (lambda: replace(gateway_facts().order, symbol="bad"), DomainTypeError),
        (lambda: replace(gateway_facts().order, side="BUY"), DomainTypeError),
        (lambda: replace(gateway_facts().order, price_type="LIMIT"), DomainTypeError),
        (lambda: replace(gateway_facts().order, status="ACKNOWLEDGED"), DomainTypeError),
        (lambda: replace(gateway_facts().order, requested_shares=Shares(0)), DomainValidationError),
        (lambda: replace(gateway_facts().order, filled_shares=Shares(101)), DomainValidationError),
        (lambda: replace(gateway_facts().order, session_date=NOW), DomainTypeError),
        (
            lambda: replace(gateway_facts().order, event_time=datetime(2026, 8, 25)),
            DomainValidationError,
        ),
        (
            lambda: replace(gateway_facts().order, received_at=datetime(2026, 8, 25)),
            DomainValidationError,
        ),
        (lambda: replace(gateway_facts().order, event_sequence=-1), DomainValidationError),
        (lambda: replace(gateway_facts().order, raw_payload_sha256="bad"), DomainValidationError),
        (lambda: replace(_fill(), broker_fill_id=""), DomainValidationError),
        (lambda: replace(_fill(), broker_order_id="\n"), DomainValidationError),
        (lambda: replace(_fill(), symbol="bad"), DomainTypeError),
        (lambda: replace(_fill(), side="BUY"), DomainTypeError),
        (lambda: replace(_fill(), status="CONFIRMED"), DomainTypeError),
        (lambda: replace(_fill(), shares=Shares(0)), DomainValidationError),
        (lambda: replace(_fill(), session_date=NOW), DomainTypeError),
        (lambda: replace(_fill(), event_time=datetime(2026, 8, 25)), DomainValidationError),
        (lambda: replace(_fill(), received_at=datetime(2026, 8, 25)), DomainValidationError),
        (lambda: replace(_fill(), event_sequence=True), DomainTypeError),
        (lambda: replace(_fill(), raw_payload_sha256="BAD"), DomainValidationError),
    ],
)
def test_broker_fact_validation_rejects_untrusted_values(
    factory: Callable[[], object], exception: type[Exception]
) -> None:
    with pytest.raises(exception):
        factory()


@pytest.mark.parametrize(
    ("change", "exception"),
    [
        ({"snapshot_id": ""}, DomainValidationError),
        ({"account": object()}, DomainTypeError),
        ({"positions": []}, DomainTypeError),
        ({"orders": [gateway_facts().order]}, DomainTypeError),
        ({"fills": [_fill()]}, DomainTypeError),
        (
            {
                "positions": (
                    BrokerPositionFact(
                        symbol=Symbol.parse("600519.SH"),
                        total_shares=Shares(1),
                        sellable_shares=Shares(1),
                        average_cost=None,
                        market_value=Money(Decimal("10")),
                    ),
                )
                * 2
            },
            DomainValidationError,
        ),
        ({"orders": (gateway_facts().order,) * 2}, DomainValidationError),
        ({"fills": (_fill(),) * 2}, DomainValidationError),
        ({"session_date": NOW}, DomainTypeError),
        ({"captured_at": datetime(2026, 8, 25)}, DomainValidationError),
        ({"broker_event_watermark": -1}, DomainValidationError),
        ({"raw_payload_sha256": "bad"}, DomainValidationError),
        ({"complete": False}, DomainValidationError),
    ],
)
def test_broker_snapshot_validation_rejects_incomplete_or_ambiguous_facts(
    change: dict[str, object], exception: type[Exception]
) -> None:
    valid = execution_snapshot().broker_snapshot
    with pytest.raises(exception):
        replace(valid, **change)


@pytest.mark.parametrize(
    ("change", "exception"),
    [
        ({"decision_id": ""}, DomainValidationError),
        ({"uquant_order_id": "bad\n"}, DomainValidationError),
        ({"symbol": "600519.SH"}, DomainTypeError),
        ({"side": "BUY"}, DomainTypeError),
        ({"requested_shares": 100}, DomainTypeError),
        ({"requested_shares": Shares(0)}, DomainValidationError),
        ({"strategy_session": NOW}, DomainTypeError),
        ({"uquant_source_sha": "bad"}, DomainValidationError),
        ({"execution_id": "exec_bad"}, DomainValidationError),
        ({"idempotency_key": "0" * 64}, DomainValidationError),
    ],
)
def test_execution_intent_identity_rejects_any_unbound_field(
    change: dict[str, object], exception: type[Exception]
) -> None:
    with pytest.raises(exception):
        replace(_intent(), **change)


def test_applied_event_and_fill_validation_is_fail_closed() -> None:
    with pytest.raises(DomainValidationError):
        AppliedEventIdentity(event_id="", fingerprint="a" * 64)
    with pytest.raises(DomainValidationError):
        AppliedEventIdentity(event_id="event-1", fingerprint="bad")
    with pytest.raises(DomainValidationError):
        AppliedFill("", "order-1", Shares(1), Price(Decimal(1)))
    with pytest.raises(DomainValidationError):
        AppliedFill("fill-1", "", Shares(1), Price(Decimal(1)))
    with pytest.raises(DomainValidationError):
        AppliedFill("fill-1", "order-1", Shares(0), Price(Decimal(1)))
    with pytest.raises(DomainTypeError):
        AppliedFill("fill-1", "order-1", Shares(1), "1")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("change", "exception"),
    [
        ({"intent": object()}, DomainTypeError),
        ({"state": "PLANNED"}, DomainTypeError),
        ({"broker_order_id": ""}, DomainValidationError),
        ({"filled_shares": 0}, DomainTypeError),
        ({"fills": []}, DomainTypeError),
        ({"filled_shares": Shares(1)}, DomainValidationError),
        (
            {
                "filled_shares": Shares(2),
                "fills": (
                    AppliedFill("fill-1", "order-1", Shares(1), Price(Decimal(1))),
                    AppliedFill("fill-1", "order-1", Shares(1), Price(Decimal(1))),
                ),
            },
            DomainValidationError,
        ),
        ({"applied_events": []}, DomainTypeError),
        (
            {
                "applied_events": (
                    AppliedEventIdentity("event-1", "a" * 64),
                    AppliedEventIdentity("event-1", "a" * 64),
                )
            },
            DomainValidationError,
        ),
        ({"submit_attempts": True}, DomainTypeError),
        ({"cancel_requests": -1}, DomainValidationError),
        ({"version": -1}, DomainValidationError),
        ({"late_fill_investigation_required": 1}, DomainTypeError),
        ({"anomalies": ["ANOMALY"]}, DomainTypeError),
        ({"anomalies": ("ANOMALY", "ANOMALY")}, DomainValidationError),
        ({"filled_shares": Shares(101)}, DomainValidationError),
        ({"state": OrderState.FILLED}, DomainValidationError),
        ({"state": OrderState.PARTIALLY_FILLED}, DomainValidationError),
    ],
)
def test_order_aggregate_rejects_corrupt_durable_state(
    change: dict[str, object], exception: type[Exception]
) -> None:
    with pytest.raises(exception):
        replace(OrderAggregate.from_intent(_intent()), **change)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: OrderEvent(event_id=""),
        lambda: BrokerAcknowledged(event_id="event", broker_order_id=""),
        lambda: SubmitOutcomeUnknown(event_id="event", diagnostic_code="bad-code"),
        lambda: UnknownResolvedNotAccepted(event_id="event", evidence_sha256="bad"),
        lambda: FillReported(
            event_id="event",
            broker_fill_id="fill",
            broker_order_id="order",
            shares=Shares(0),
            price=Price(Decimal(1)),
        ),
        lambda: CancelConfirmed(event_id="event", broker_order_id=""),
        lambda: BrokerRejected(event_id="event", reason_code="bad"),
        lambda: OrderExpired(event_id="event", reason_code="bad"),
    ],
)
def test_order_event_validation_rejects_noncanonical_facts(factory: Callable[[], object]) -> None:
    with pytest.raises((DomainTypeError, DomainValidationError)):
        factory()


def test_event_fingerprint_rejects_untyped_and_unserializable_events() -> None:
    with pytest.raises(DomainTypeError):
        event_fingerprint(object())  # type: ignore[arg-type]

    class UnsupportedEvent(OrderEvent):
        value: object = object()

    unsupported = UnsupportedEvent(event_id="event")
    object.__setattr__(unsupported, "value", object())
    assert len(event_fingerprint(unsupported)) == 64
