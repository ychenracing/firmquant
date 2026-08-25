from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

from uquant.types import (
    AccountOrder,
    AccountState,
    AttributionMechanism,
    Lifecycle,
    OrderStatus,
    OriginSubsystem,
    PendingOrder,
    derive_attribution_event_id,
)

from firmquant.domain.broker_facts import (
    AccountType,
    BrokerAccountFact,
    BrokerFillFact,
    BrokerOrderFact,
    BrokerOrderStatus,
    BrokerPositionFact,
    BrokerSnapshot,
    FillStatus,
    PriceType,
    Side,
)
from firmquant.domain.values import Money, Price, Shares, Symbol

UNIVERSE_SHA256 = "03f42c5066fb8e1c7b2f8e1b7dd38d508d8053f548ebb5596317ce587d7cffd0"


def open_buy_account() -> AccountState:
    signal_date = "2026-01-05"
    symbol = "sz300308"
    target_weight = 0.5
    lifecycle = Lifecycle.CORE.value
    origin = OriginSubsystem.LEADER.value
    mechanism = AttributionMechanism.LEADER_SELECTION.value
    identity = {
        "event_id": derive_attribution_event_id(
            signal_date=signal_date,
            symbol=symbol,
            target_weight=target_weight,
            lifecycle=lifecycle,
            origin_lifecycle=lifecycle,
            origin_subsystem=origin,
            mechanism=mechanism,
            replaces_symbol=None,
            industry_at_entry="optical",
            industry_manifest_sha256=UNIVERSE_SHA256,
            reduction_policy="FIFO",
            reason_code="strategy_target",
            exit_kind="strategy",
        ),
        "origin_subsystem": origin,
        "mechanism": mechanism,
        "origin_lifecycle": lifecycle,
        "replaces_symbol": None,
        "industry_at_entry": "optical",
        "industry_manifest_sha256": UNIVERSE_SHA256,
    }
    pending = PendingOrder(
        signal_date=signal_date,
        symbol=symbol,
        side="BUY",
        target_weight=target_weight,
        reason="entry",
        lifecycle=lifecycle,
        remaining_shares=100,
        order_id="O000000001",
        **identity,
    )
    order = AccountOrder(
        order_id="O000000001",
        signal_date=signal_date,
        submitted_date=signal_date,
        symbol=symbol,
        side="BUY",
        target_weight=target_weight,
        reason="entry",
        lifecycle=lifecycle,
        status=OrderStatus.OPEN.value,
        requested_shares=100,
        remaining_shares=100,
        last_update_date=signal_date,
        **identity,
    )
    return AccountState(
        initial_cash=2_000.0,
        cash=2_000.0,
        pending_orders=[pending],
        order_ledger=[order],
        next_order_sequence=2,
        operating_peak=2_000.0,
        capital_peak=2_000.0,
    )


def completed_buy_snapshot() -> BrokerSnapshot:
    session = date(2026, 1, 6)
    event_time = datetime(2026, 1, 6, 2, tzinfo=UTC)
    order = BrokerOrderFact(
        broker_order_id="broker-order-1",
        client_order_id="O000000001",
        symbol=Symbol.parse("sz300308"),
        side=Side.BUY,
        price_type=PriceType.LIMIT,
        status=BrokerOrderStatus.FILLED,
        requested_shares=Shares(100),
        filled_shares=Shares(100),
        limit_price=Price(Decimal("10")),
        session_date=session,
        event_time=event_time,
        received_at=event_time,
        event_sequence=2,
        raw_payload_sha256="1" * 64,
    )
    fill = BrokerFillFact(
        broker_fill_id="broker-fill-1",
        broker_order_id=order.broker_order_id,
        symbol=order.symbol,
        side=order.side,
        status=FillStatus.CONFIRMED,
        shares=Shares(100),
        price=Price(Decimal("10")),
        commission=Money(Decimal("5")),
        stamp_duty=Money(Decimal("0")),
        transfer_fee=Money(Decimal("0.1")),
        session_date=session,
        event_time=event_time,
        received_at=event_time,
        event_sequence=1,
        raw_payload_sha256="2" * 64,
    )
    return BrokerSnapshot(
        snapshot_id="snapshot-completed-buy",
        account=BrokerAccountFact(
            account_id_hash="a" * 64,
            account_type=AccountType.CASH,
            available_cash=Money(Decimal("994.9")),
            total_assets=Money(Decimal("1994.9")),
        ),
        positions=(
            BrokerPositionFact(
                symbol=order.symbol,
                total_shares=Shares(100),
                sellable_shares=Shares(0),
                average_cost=Price(Decimal("10.051")),
                market_value=Money(Decimal("1000")),
            ),
        ),
        orders=(order,),
        fills=(fill,),
        session_date=session,
        captured_at=event_time,
        broker_event_watermark=2,
        raw_payload_sha256="3" * 64,
        complete=True,
    )


def cancelled_buy_snapshot() -> BrokerSnapshot:
    completed = completed_buy_snapshot()
    order = replace(
        completed.orders[0],
        status=BrokerOrderStatus.CANCELLED,
        filled_shares=Shares(0),
    )
    return replace(
        completed,
        snapshot_id="snapshot-cancelled-buy",
        account=replace(
            completed.account,
            available_cash=Money(Decimal("2000")),
            total_assets=Money(Decimal("2000")),
        ),
        positions=(),
        orders=(order,),
        fills=(),
        raw_payload_sha256="4" * 64,
    )
