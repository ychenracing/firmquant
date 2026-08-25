from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal

import pytest

from firmquant.broker.gateway import BrokerGateway
from firmquant.broker.paper import PaperBroker, PaperCallbackDeliveryError
from firmquant.domain.broker_facts import (
    AccountType,
    BrokerAccountFact,
    BrokerOrderStatus,
    BrokerPositionFact,
    MarketSessionStatus,
    SecurityStatus,
    Side,
)
from firmquant.domain.values import Money, Price, Shares
from firmquant.execution.policy import ExecutionPolicy, FeeSchedule, FillModel
from tests.fixtures.broker_contract import (
    assert_read_gateway_contract,
    gateway_facts,
    order_command,
)


def _policy(
    *, participation: str = "0.005", slippage_bps: str = "0"
) -> ExecutionPolicy:
    return ExecutionPolicy(
        fill_model=FillModel(
            max_volume_participation=Decimal(participation),
            slippage_bps=Decimal(slippage_bps),
        ),
        fee_schedule=FeeSchedule(
            commission_rate=Decimal("0.0003"),
            minimum_commission=Decimal("5.00"),
            stamp_duty_rate=Decimal("0.001"),
            transfer_fee_rate=Decimal("0.00001"),
            fee_quantum=Decimal("0.01"),
        ),
    )


def _paper(
    *,
    policy: ExecutionPolicy | None = None,
    account: BrokerAccountFact | None = None,
    position: BrokerPositionFact | None = None,
    instrument_status: SecurityStatus = SecurityStatus.TRADING,
    limits_present: bool = True,
) -> PaperBroker:
    facts = gateway_facts()
    instrument = replace(
        facts.instrument,
        status=instrument_status,
        lower_limit=facts.instrument.lower_limit if limits_present else None,
        upper_limit=facts.instrument.upper_limit if limits_present else None,
    )
    return PaperBroker(
        account=account or facts.account,
        positions=() if position is None else (position,),
        instruments=(instrument,),
        quotes=(facts.quote,),
        market_status=MarketSessionStatus.OPEN,
        policy=policy or _policy(),
        clock=lambda: facts.quote.received_at,
    )


def test_paper_broker_passes_shared_read_contract() -> None:
    broker = _paper()
    assert isinstance(broker, BrokerGateway)
    assert_read_gateway_contract(broker)


def test_paper_fill_respects_volume_participation_and_is_partial() -> None:
    broker = _paper(policy=_policy(participation="0.005"))
    broker.connect()

    order = broker.submit_order(order_command(shares=10_000, identity="volume"))

    assert order.status is BrokerOrderStatus.PARTIALLY_FILLED
    assert order.filled_shares == Shares(500)
    assert broker.query_fills()[0].shares == Shares(500)
    assert broker.query_account().available_cash.value == Decimal("94944.95")

    exhausted = broker.match(order.broker_order_id)
    assert exhausted.fill is None
    assert exhausted.reason_code == "VOLUME_CAPACITY_EXHAUSTED"
    quote = gateway_facts().quote
    broker.set_quote(
        replace(
            quote,
            volume=Shares(200_000),
            sequence=quote.sequence + 1,
        )
    )
    next_slice = broker.match(order.broker_order_id)
    assert next_slice.fill is not None
    assert next_slice.fill.shares == Shares(500)


def test_paper_slippage_and_fees_update_cash_without_binary_float() -> None:
    broker = _paper(policy=_policy(participation="1", slippage_bps="10"))
    broker.connect()

    order = broker.submit_order(
        order_command(shares=100, limit_price="10.20", identity="slippage")
    )
    fill = broker.query_fills()[0]

    assert order.status is BrokerOrderStatus.FILLED
    assert fill.price.value == Decimal("10.11")
    assert fill.commission.value == Decimal("5.00")
    assert broker.query_account().available_cash.value == Decimal("98983.99")
    assert broker.query_account().available_cash.value >= 0


def test_paper_buy_is_t_plus_one_and_cannot_be_sold_same_day() -> None:
    broker = _paper(policy=_policy(participation="1"))
    broker.connect()
    broker.submit_order(order_command(identity="buy-first"))
    position = broker.query_positions()[0]
    assert position.total_shares == Shares(100)
    assert position.sellable_shares == Shares(0)

    sell = broker.submit_order(
        order_command(side=Side.SELL, identity="same-day-sell")
    )
    assert sell.status is BrokerOrderStatus.REJECTED
    assert broker.reason_for(sell.broker_order_id) == "T1_SELLABLE_EXCEEDED"
    assert broker.query_positions()[0].total_shares == Shares(100)


def test_paper_rejects_suspension_and_missing_broker_price_limits() -> None:
    suspended = _paper(instrument_status=SecurityStatus.SUSPENDED)
    suspended.connect()
    suspended_order = suspended.submit_order(order_command(identity="suspended"))
    assert suspended_order.status is BrokerOrderStatus.REJECTED
    assert suspended.reason_for(suspended_order.broker_order_id) == "INSTRUMENT_NOT_TRADING"

    no_limits = _paper(limits_present=False)
    no_limits.connect()
    unbounded_order = no_limits.submit_order(order_command(identity="no-limits"))
    assert unbounded_order.status is BrokerOrderStatus.REJECTED
    assert no_limits.reason_for(unbounded_order.broker_order_id) == "PRICE_LIMIT_FACT_MISSING"


def test_paper_rejects_invalid_trading_unit_and_price_boundary() -> None:
    broker = _paper()
    broker.connect()

    odd_lot = broker.submit_order(order_command(shares=50, identity="odd-buy"))
    out_of_bounds = broker.submit_order(
        order_command(limit_price="12.00", identity="out-of-bounds")
    )

    assert odd_lot.status is BrokerOrderStatus.REJECTED
    assert broker.reason_for(odd_lot.broker_order_id) == "TRADING_UNIT_INVALID"
    assert out_of_bounds.status is BrokerOrderStatus.REJECTED
    assert broker.reason_for(out_of_bounds.broker_order_id) == "LIMIT_PRICE_OUT_OF_BOUNDS"


def test_paper_blocks_buy_at_upper_limit_without_guessing_percentage() -> None:
    broker = _paper()
    facts = gateway_facts()
    upper = facts.quote.upper_limit
    assert upper is not None
    broker.set_quote(
        replace(
            facts.quote,
            last_price=upper,
            bid_price=upper,
            ask_price=upper,
            sequence=facts.quote.sequence + 1,
        )
    )
    broker.connect()

    order = broker.submit_order(
        order_command(limit_price=upper.canonical, identity="upper-limit")
    )

    assert order.status is BrokerOrderStatus.ACKNOWLEDGED
    assert order.filled_shares == Shares(0)
    assert broker.reason_for(order.broker_order_id) == "UPPER_LIMIT_BUY_BLOCKED"


def test_paper_blocks_sell_at_lower_limit_and_enforces_sellable_quantity() -> None:
    facts = gateway_facts()
    position = BrokerPositionFact(
        symbol=facts.instrument.symbol,
        total_shares=Shares(100),
        sellable_shares=Shares(100),
        average_cost=Price(Decimal("10")),
        market_value=Money(Decimal("1010")),
    )
    account = BrokerAccountFact(
        account_id_hash="a" * 64,
        account_type=AccountType.CASH,
        available_cash=Money(Decimal("1000")),
        total_assets=Money(Decimal("2010")),
    )
    broker = _paper(account=account, position=position)
    lower = facts.quote.lower_limit
    assert lower is not None
    broker.set_quote(
        replace(
            facts.quote,
            last_price=lower,
            bid_price=lower,
            ask_price=lower,
            sequence=facts.quote.sequence + 1,
        )
    )
    broker.connect()

    order = broker.submit_order(
        order_command(
            side=Side.SELL,
            limit_price=lower.canonical,
            identity="lower-limit",
        )
    )

    assert order.status is BrokerOrderStatus.ACKNOWLEDGED
    assert order.filled_shares == Shares(0)
    assert broker.reason_for(order.broker_order_id) == "LOWER_LIMIT_SELL_BLOCKED"
    assert broker.query_positions()[0].total_shares == Shares(100)


def test_paper_ids_and_results_are_deterministic() -> None:
    left = _paper(policy=_policy(participation="1"))
    right = _paper(policy=_policy(participation="1"))
    left.connect()
    right.connect()

    left_order = left.submit_order(order_command(identity="deterministic"))
    right_order = right.submit_order(order_command(identity="deterministic"))

    assert left_order.broker_order_id == right_order.broker_order_id
    assert left.query_fills()[0].broker_fill_id == right.query_fills()[0].broker_fill_id
    assert left.state_sha256 == right.state_sha256


def test_paper_cancels_only_an_open_system_order_idempotently() -> None:
    broker = _paper()
    broker.connect()
    acknowledged = broker.submit_order(
        order_command(limit_price="9.50", identity="resting-order")
    )
    assert acknowledged.status is BrokerOrderStatus.ACKNOWLEDGED

    cancelled = broker.cancel_order(acknowledged.broker_order_id)
    assert cancelled.status is BrokerOrderStatus.CANCELLED
    assert broker.cancel_order(acknowledged.broker_order_id) == cancelled


def test_paper_callback_failure_preserves_facts_and_blocks_further_writes() -> None:
    broker = _paper(policy=_policy(participation="1"))
    broker.connect()

    def fail_callback(_: Mapping[str, object]) -> None:
        raise RuntimeError("event queue unavailable")

    broker.subscribe(fail_callback)
    with pytest.raises(PaperCallbackDeliveryError, match="reconcile"):
        broker.submit_order(order_command(identity="callback-failure"))

    assert len(broker.query_orders()) == 1
    with pytest.raises(PaperCallbackDeliveryError, match="reconcile"):
        broker.submit_order(order_command(identity="blocked-after-callback"))
    assert len(broker.query_orders()) == 1

    replayed: list[Mapping[str, object]] = []
    broker.subscribe(replayed.append)
    broker.submit_order(order_command(identity="recovered-callback"))
    assert replayed[0]["event_type"] == "ORDER"
    assert len(broker.query_orders()) == 2
