from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from firmquant.broker.gateway import BrokerOrderCommand
from firmquant.config import Mode
from firmquant.domain.broker_facts import (
    AccountType,
    InstrumentFact,
    MarketSessionStatus,
    PriceType,
    QuoteFact,
    SecurityStatus,
    SecurityType,
    Side,
)
from firmquant.domain.states import RuntimeState
from firmquant.domain.values import Money, Price, Shares, Symbol
from firmquant.risk.gate import ExecutionRiskContext, RiskCommand, RiskLimits

NOW = datetime(2026, 8, 25, 1, 31, tzinfo=UTC)
SESSION = date(2026, 8, 25)
SYMBOL = Symbol.parse("sz300308")


def risk_limits() -> RiskLimits:
    return RiskLimits(
        max_order_notional=Money(Decimal("100000")),
        max_daily_submitted_notional=Money(Decimal("500000")),
        max_daily_filled_notional=Money(Decimal("500000")),
        max_symbol_notional=Money(Decimal("200000")),
        max_total_gross_notional=Money(Decimal("800000")),
        max_open_orders=10,
        max_consecutive_rejections=3,
        max_disconnect_duration=timedelta(seconds=30),
        max_order_lifetime=timedelta(minutes=10),
        max_replacements=2,
        max_submit_count_window=10,
        max_cancel_count_window=10,
        max_quote_age=timedelta(seconds=5),
        max_clock_drift=timedelta(seconds=2),
        max_price_deviation_bps=Decimal("100"),
        max_equity_change_fraction=Decimal("0.10"),
        max_intraday_loss_fraction=Decimal("0.05"),
        max_capital_drawdown_fraction=Decimal("0.15"),
    )


def instrument() -> InstrumentFact:
    return InstrumentFact(
        symbol=SYMBOL,
        security_type=SecurityType.EQUITY,
        status=SecurityStatus.TRADING,
        trading_unit=Shares(100),
        price_tick=Price(Decimal("0.01")),
        price_precision=2,
        lower_limit=Price(Decimal("9")),
        upper_limit=Price(Decimal("11")),
        session_date=SESSION,
        observed_at=NOW,
    )


def quote(*, volume: int = 100_000) -> QuoteFact:
    return QuoteFact(
        symbol=SYMBOL,
        last_price=Price(Decimal("10")),
        previous_close=Price(Decimal("10")),
        bid_price=Price(Decimal("10")),
        ask_price=Price(Decimal("10")),
        volume=Shares(volume),
        turnover=Money(Decimal(volume * 10)),
        lower_limit=Price(Decimal("9")),
        upper_limit=Price(Decimal("11")),
        market_status=MarketSessionStatus.OPEN,
        sequence=1,
        session_date=SESSION,
        event_time=NOW,
        received_at=NOW,
    )


def risk_command(
    *,
    side: Side = Side.BUY,
    requested: int = 100,
    authorized: int = 500,
    price: str = "10",
) -> RiskCommand:
    identity = hashlib.sha256(
        f"{side.value}:{requested}:{authorized}:{price}".encode()
    ).hexdigest()
    command = BrokerOrderCommand(
        execution_id="exec_" + identity,
        idempotency_key=hashlib.sha256(f"idem:{identity}".encode()).hexdigest(),
        client_order_id="O-RISK-1",
        symbol=SYMBOL,
        side=side,
        price_type=PriceType.LIMIT,
        requested_shares=Shares(requested),
        limit_price=Price(Decimal(price)),
        strategy_session=date(2026, 8, 24),
    )
    return RiskCommand(
        command=command,
        uquant_authorized_shares=Shares(authorized),
        estimated_fees=Money(Decimal("5")),
    )


def risk_context() -> ExecutionRiskContext:
    return ExecutionRiskContext(
        mode=Mode.LIVE,
        runtime_state=RuntimeState.READY,
        now=NOW,
        account_type=AccountType.CASH,
        available_cash=Money(Decimal("100000")),
        total_assets=Money(Decimal("100000")),
        position=None,
        instrument=instrument(),
        quote=quote(),
        market_status=MarketSessionStatus.OPEN,
        canonical_universe=frozenset({SYMBOL}),
        deployment_allowlist=frozenset({SYMBOL}),
        uquant_target_shares=Shares(500),
        uquant_target_weight=Decimal("0.05"),
        uquant_target_gross=Decimal("0.50"),
        uquant_target_gross_cap=Decimal("1.00"),
        freeze_new_risk=False,
        actual_symbol_notional=Money(Decimal("0")),
        actual_gross_notional=Money(Decimal("0")),
        daily_submitted_notional=Money(Decimal("0")),
        daily_filled_notional=Money(Decimal("0")),
        open_order_count=0,
        consecutive_rejections=0,
        broker_connected=True,
        disconnect_duration=timedelta(0),
        existing_order_age=None,
        replacement_count=0,
        submit_count_window=0,
        cancel_count_window=0,
        uquant_max_volume_participation=Decimal("0.005"),
        equity_change_fraction=Decimal("0"),
        intraday_loss_fraction=Decimal("0"),
        capital_drawdown_fraction=Decimal("0"),
        reconciliation_healthy=True,
        external_active_order_count=0,
        unexplained_position_change=False,
        corporate_action_suspected=False,
        clock_drift=timedelta(0),
        data_identity_matches=True,
        config_identity_matches=True,
        unresolved_order_count=0,
        kill_switch_tripped=False,
        auction_allowed=False,
        limits=risk_limits(),
    )
